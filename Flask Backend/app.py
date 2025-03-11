import os
import zipfile
from flask import Flask, request, jsonify, render_template
from ultralytics import YOLO
from easyocr import Reader
import cv2
import pandas as pd
from fuzzywuzzy import fuzz
import re

# Flask application setup
app = Flask(__name__)

class AadhaarVerificationSystem:
    def __init__(self, upload_folder, classifier_path, detector_path):
        self.upload_folder = upload_folder
        self.extract_folder = os.path.join(upload_folder, 'extracted_files')
        os.makedirs(self.extract_folder, exist_ok=True)

        # Initialize models
        self.classifier = YOLO(classifier_path)
        self.detector = YOLO(detector_path)
        self.ocr_reader = Reader(['en'])

    def clean_text(self, text):
        return ''.join(e for e in str(text) if e.isalnum()).lower()

    def clean_uid(self, uid):
        return ''.join(filter(str.isdigit, str(uid)))

    def clean_address(self, address):
        address = address.lower()
        address = re.sub(r'\s+', ' ', address)  # Remove extra spaces
        address = re.sub(r'(marg|lane|township|block|street)', '', address)  # Remove common terms
        return address

    def is_aadhaar_card(self, image_path):
        try:
            prediction = self.classifier.predict(source=image_path)
            class_index = prediction[0].probs.top1
            class_name = prediction[0].names[class_index]
            confidence = prediction[0].probs.top1conf.item()

            return class_name.lower() in ["aadhar", "aadhaar"], confidence
        except Exception as e:
            print(f"Classification error: {str(e)}")
            return False, 0

    def detect_fields(self, image_path):
        try:
            results = self.detector(image_path)[0]
            fields = {}

            image = cv2.imread(image_path)
            for box in results.boxes:
                class_id = int(box.cls[0])
                conf = float(box.conf[0])
                label = self.detector.names[class_id]
                coords = box.xyxy[0].cpu().numpy().astype(int)

                if conf > 0.5:
                    x1, y1, x2, y2 = coords
                    cropped_roi = image[y1:y2, x1:x2]

                    # Preprocess for OCR
                    gray = cv2.cvtColor(cropped_roi, cv2.COLOR_BGR2GRAY)
                    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                    text = self.ocr_reader.readtext(thresh, detail=0)
                    fields[label] = ' '.join(text) if text else None

            return fields
        except Exception as e:
            print(f"Field detection error: {str(e)}")
            return {}

    def match_names(self, extracted_name, excel_name):
        return fuzz.ratio(self.clean_text(extracted_name), self.clean_text(excel_name))

    def match_addresses(self, extracted_address, row):
        address_components = ['Street Road Name', 'City', 'State', 'PINCODE']
        full_address = ' '.join([str(row[comp]) for comp in address_components if row[comp]])

        cleaned_extracted_address = self.clean_address(extracted_address)
        cleaned_full_address = self.clean_address(full_address)

        address_score = fuzz.partial_ratio(cleaned_extracted_address, cleaned_full_address)

        extracted_pincode = re.sub(r'\D', '', extracted_address)
        if extracted_pincode == row['PINCODE']:
            address_score = 100

        return address_score, full_address

    def compare_with_excel(self, fields, excel_path):
        try:
            excel_data = pd.read_excel(excel_path)
            uid = fields.get("uid")
            extracted_name = fields.get("name", "N/A")
            extracted_address = fields.get("address")

            if not uid:
                return [{"status": "Rejected", "reason": "UID not found in image."}]

            uid_cleaned = self.clean_uid(uid)
            best_match = None
            highest_score = 0

            for _, row in excel_data.iterrows():
                excel_uid_cleaned = self.clean_uid(row.get("UID", ""))
                name_score = self.match_names(extracted_name, row.get("Name", "")) if extracted_name != "N/A" else 0
                address_score, full_address = self.match_addresses(extracted_address, row) if extracted_address else (0, None)
                uid_score = fuzz.ratio(uid_cleaned, excel_uid_cleaned)

                overall_score = (name_score + address_score + uid_score) / 3 

                status = "Accepted" if overall_score >= 80 else "Rejected"

                if overall_score > highest_score:
                    highest_score = overall_score
                    best_match = {
                        "SrNo": row.get("SrNo"),
                        "Name": row.get("Name"),
                        "Extracted Name": extracted_name,
                        "UID": row.get("UID"),
                        "Address Match Score": address_score if extracted_address else None,
                        "Address Reference": full_address,
                        "Name Match Score": name_score if extracted_name != "N/A" else None,
                        "UID Match Score": uid_score,
                        "Overall Match Score": overall_score,
                        "status": status,
                        "reason": "Matching score is less than expected."
                    }

            return [best_match] if best_match else [{"status": "Rejected", "reason": "No matching record found."}]

        except Exception as e:
            print(f"Excel comparison error: {str(e)}")
            return [{"status": "Error", "reason": str(e)}]

    def process_zip_file(self, zip_path, excel_path):
        results = []
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(self.extract_folder)

            for root, _, files in os.walk(self.extract_folder):
                for file in files:
                    if file.lower().endswith(('.png', '.jpg', '.jpeg')):
                        image_path = os.path.join(root, file)
                        is_aadhar, confidence = self.is_aadhaar_card(image_path)

                        if is_aadhar:
                            fields = self.detect_fields(image_path)
                            fields["filename"] = file
                            match_results = self.compare_with_excel(fields, excel_path)
                            results.append({
                                'filename': file,
                                'is_aadhar': is_aadhar,
                                'confidence': confidence,
                                'fields': fields,
                                'match_results': match_results
                            })
                        else:
                            results.append({
                                'filename': file,
                                'is_aadhar': is_aadhar,
                                'confidence': confidence,
                                'reason': "Not an Aadhaar card."
                            })
            return results

        except Exception as e:
            print(f"Zip processing error: {str(e)}")
            raise

UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Initialize Aadhaar verification system
verifier = AadhaarVerificationSystem(
    upload_folder=UPLOAD_FOLDER,
    classifier_path=os.path.join(os.getcwd(), 'models', 'classifier.pt'),
    detector_path=os.path.join(os.getcwd(), 'models', 'detector.pt')
)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload_files():
    if request.method == 'POST':
        if 'zipfile' not in request.files or 'excelfile' not in request.files:
            return jsonify({"error": "Both files are required."}), 400

        zip_file = request.files['zipfile']
        excel_file = request.files['excelfile']

        zip_path = os.path.join(verifier.upload_folder, zip_file.filename)
        excel_path = os.path.join(verifier.upload_folder, excel_file.filename)

        zip_file.save(zip_path)
        excel_file.save(excel_path)

        try:
            results = verifier.process_zip_file(zip_path, excel_path)
            return jsonify({"results": results, "success": True})

        except Exception as e:
            return jsonify({"error": str(e)}), 500

        finally:
            os.remove(zip_path) if os.path.exists(zip_path) else None
            os.remove(excel_path) if os.path.exists(excel_path) else None

    return render_template('upload.html')

@app.route('/analytics')
def analytics_dashboard():
    file_path = "C:/Users/gaura/Downloads/aadhaar_verification_results (2).xlsx"  # Adjust if needed
    df = pd.read_excel(file_path)

    total_files = len(df)
    processed_files = df["Status"].notna().sum()

    status_counts = df["Status"].value_counts().to_dict()
    status_distribution = {
        status: {"count": count, "percentage": round((count / total_files) * 100, 2)}
        for status, count in status_counts.items()
    }

    match_scores = df["Overall Match Score (%)"].dropna()
    avg_score = round(match_scores.mean(), 2) if not match_scores.empty else 0
    score_deviation = round(match_scores.std(), 2) if not match_scores.empty else 0

    bins = [0, 50, 70, 85, 100]
    labels = ["0-50%", "51-70%", "71-85%", "86-100%"]
    df["Score Range"] = pd.cut(match_scores, bins=bins, labels=labels, include_lowest=True)
    score_ranges_counts = df["Score Range"].value_counts().to_dict()
    score_ranges_distribution = {
        key: {"count": count, "percentage": round((count / total_files) * 100, 2)}
        for key, count in score_ranges_counts.items()
    }

    analytics_data = {
        "total_files": total_files,
        "processed_files": processed_files,
        "verification_stats": status_distribution,
        "match_score_analysis": {
            "avg_score": avg_score,
            "score_deviation": score_deviation,
        },
        "score_ranges": score_ranges_distribution,
    }

    return render_template('analytics.html', analytics_data=analytics_data)


if __name__ == '__main__':
    app.run(debug=True)
