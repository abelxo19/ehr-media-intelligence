"""Generate the synthetic, intentionally messy EHR export used by this project.

Every patient, MRN and clinical detail here is fictional. I wrote the clinical
content by hand (rather than random text) so the summaries and search results
are meaningful, then deliberately injected the data-quality problems the
cleaning pipeline has to deal with:

* three different date formats per file, plus a few ambiguous / impossible dates
* gender as M/F/1/2/"male"/blank
* MRNs as "MRN-0012345", "12345", "00-12345", "mrn 0012345"
* the same patient exported twice with different formatting
* the same patient under two different MRNs (a transposition typo)
* exact duplicate documents and duplicate lab rows
* missing dates, missing document types, empty document bodies
* OCR-style noise in the "scanned" notes (hyphenated line breaks, stray spaces)

Run:  python scripts/generate_synthetic_data.py
Outputs: data/raw/ehr_export.json, data/raw/lab_results.csv, data/raw/scanned_notes.txt
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "raw"

# --------------------------------------------------------------------------------------
# 1. JSON "EHR export": patients + encounters + documents
# --------------------------------------------------------------------------------------
EHR_EXPORT = {
    "export_id": "SYNTH-EXPORT-2026-11",
    "source_system": "Simulated Epic Clarity extract (synthetic)",
    "patients": [
        {
            "mrn": "MRN-0012345",
            "name": {"given": "Maria", "family": "Gonzalez"},
            "dob": "03/14/1958",
            "sex": "F",
            "phone": "555-0101",
            "encounters": [
                {
                    "encounter_id": "ENC-1001",
                    "class": "inpatient",
                    "start": "2026-02-10",
                    "end": "2026-02-14",
                    "documents": [
                        {
                            "doc_id": "D-1001-A",
                            "type": "Discharge Summary",
                            "date": "02/14/2026",
                            "author": "Dr. Helen Brooks",
                            "text": (
                                "Admitted with hyperglycemia (glucose 412 mg/dL) and acute kidney injury on "
                                "chronic kidney disease stage 3. History of type 2 diabetes mellitus for 18 years, "
                                "hypertension. Treated with IV fluids and insulin infusion, transitioned to basal-bolus "
                                "insulin. Creatinine improved from 2.4 to 1.7 mg/dL. Metformin discontinued due to eGFR "
                                "below 30. Discharged home with glucose meter teaching. Follow up with nephrology in 2 weeks."
                            ),
                        },
                        {
                            "doc_id": "D-1001-B",
                            "type": "Renal Ultrasound",
                            "date": "2026-02-11",
                            "author": "Dr. Samuel Reyes, Radiology",
                            "text": (
                                "IMPRESSION: Bilateral echogenic kidneys consistent with medical renal disease. "
                                "Right kidney 9.1 cm, left kidney 9.4 cm. No hydronephrosis. No renal calculi."
                            ),
                        },
                    ],
                },
                {
                    "encounter_id": "ENC-1002",
                    "class": "ambulatory",
                    "start": "2026-03-02",
                    "documents": [
                        {
                            "doc_id": "D-1002-A",
                            "type": "Consult Note",
                            "date": "March 2, 2026",
                            "author": "Dr. Priya Natarajan, Nephrology",
                            "text": (
                                "Nephrology consult for CKD stage 3b with recent AKI. Diabetic nephropathy suspected; "
                                "urine albumin-to-creatinine ratio 480 mg/g. Started empagliflozin is deferred until eGFR "
                                "stabilizes. Recommend ACE inhibitor titration and repeat BMP in 1 week. Avoid NSAIDs."
                            ),
                        },
                        # Exact duplicate of the previous document (same id) - should be dropped.
                        {
                            "doc_id": "D-1002-A",
                            "type": "Consult Note",
                            "date": "March 2, 2026",
                            "author": "Dr. Priya Natarajan, Nephrology",
                            "text": (
                                "Nephrology consult for CKD stage 3b with recent AKI. Diabetic nephropathy suspected; "
                                "urine albumin-to-creatinine ratio 480 mg/g. Started empagliflozin is deferred until eGFR "
                                "stabilizes. Recommend ACE inhibitor titration and repeat BMP in 1 week. Avoid NSAIDs."
                            ),
                        },
                    ],
                },
            ],
        },
        {
            "mrn": "23456",
            "name": {"given": "James", "family": "Okafor"},
            "birthDate": "1972-11-02",
            "gender": "male",
            "encounters": [
                {
                    "encounter_id": "ENC-2001",
                    "class": "emergency",
                    "start": "2026-05-18",
                    "documents": [
                        {
                            "doc_id": "D-2001-A",
                            "type": "ED Note",
                            "date": "2026-05-18T07:42:00",
                            "author": "Dr. Karen Liu, Emergency Medicine",
                            "text": (
                                "53-year-old male with 2 hours of substernal chest pressure radiating to left arm, "
                                "diaphoresis. ECG shows 2 mm ST depression in V4-V6. Initial high-sensitivity troponin "
                                "88 ng/L, repeat 240 ng/L. Diagnosis: NSTEMI. Aspirin, heparin drip, atorvastatin 80 mg "
                                "started. Cardiology consulted for catheterization."
                            ),
                        },
                        {
                            "doc_id": "D-2001-B",
                            "type": "Cardiac Catheterization Report",
                            "date": "05/19/2026",
                            "author": "Dr. Marcus Feld, Interventional Cardiology",
                            "text": (
                                "Left heart catheterization: 90% stenosis of proximal left anterior descending artery, "
                                "treated with one drug-eluting stent with TIMI 3 flow. Left ventricular ejection fraction "
                                "estimated 45%. Recommend dual antiplatelet therapy for 12 months."
                            ),
                        },
                        {
                            "doc_id": "D-2001-C",
                            "type": "Echocardiogram",
                            "date": "2026-05-20",
                            "author": "Dr. Marcus Feld",
                            "text": (
                                "Transthoracic echocardiogram: LVEF 45% with hypokinesis of the anterior wall and apex. "
                                "Mild mitral regurgitation. No pericardial effusion."
                            ),
                        },
                    ],
                }
            ],
        },
        {
            "mrn": "00-34567",
            "name": {"given": "Linda", "family": "Chen"},
            "date_of_birth": "21-Jul-1985",
            "sex": "2",
            "encounters": [
                {
                    "encounter_id": "ENC-3001",
                    "class": "emergency",
                    "start": "2026-01-07",
                    "documents": [
                        {
                            "doc_id": "D-3001-A",
                            "type": "ED Note",
                            "date": "01/07/2026",
                            "author": "Dr. Omar Haddad",
                            "text": (
                                "Moderate persistent asthma with acute exacerbation after viral URI. SpO2 91% on room air, "
                                "diffuse expiratory wheeze. Peak flow 45% predicted. Treated with nebulized albuterol and "
                                "ipratropium x3, oral prednisone 40 mg. Improved to SpO2 96%. Discharged on 5-day prednisone "
                                "burst and step-up to medium-dose ICS-formoterol."
                            ),
                        },
                        {
                            "doc_id": "D-3001-B",
                            "type": "Chest X-Ray",
                            "date": "2026-01-07",
                            "author": "Dr. Samuel Reyes, Radiology",
                            "text": (
                                "PA and lateral chest radiograph. Hyperinflated lungs. No focal consolidation, effusion or "
                                "pneumothorax. IMPRESSION: Findings compatible with reactive airway disease. No pneumonia."
                            ),
                        },
                    ],
                }
            ],
        },
        {
            "mrn": "mrn 0045678",
            "name": {"given": "Robert", "family": "Williams"},
            "dob": "1949-01-30",
            "sex": "M",
            "encounters": [
                {
                    "encounter_id": "ENC-4001",
                    "class": "inpatient",
                    "start": "2025-12-01",
                    "end": "2025-12-06",
                    "documents": [
                        {
                            "doc_id": "D-4001-A",
                            "type": "Discharge Summary",
                            # Missing date on purpose -> filled from encounter end date.
                            "author": "Dr. Helen Brooks",
                            "text": (
                                "Admitted for acute decompensated heart failure with reduced ejection fraction (LVEF 25%). "
                                "Presented with orthopnea, 4 kg weight gain and bilateral leg edema. NT-proBNP 6,800 pg/mL. "
                                "Diuresed with IV furosemide, net negative 5.2 L. Started sacubitril-valsartan and "
                                "spironolactone. Atrial fibrillation rate controlled with metoprolol; apixaban continued. "
                                "Discharge weight 81 kg. Fluid restriction 1.5 L/day."
                            ),
                        },
                        {
                            "doc_id": "D-4001-B",
                            "type": "Chest X-Ray",
                            "date": "12/01/2025",
                            "author": "Dr. Samuel Reyes, Radiology",
                            "text": (
                                "Portable AP chest. Cardiomegaly with cephalization of pulmonary vasculature, bilateral "
                                "interstitial edema and small bilateral pleural effusions. IMPRESSION: Pulmonary edema."
                            ),
                        },
                    ],
                },
                {
                    "encounter_id": "ENC-4002",
                    "class": "ambulatory",
                    "start": "2026-01-15",
                    "documents": [
                        {
                            "doc_id": "D-4002-A",
                            "type": "Progress Note",
                            "date": "15-Jan-2026",
                            "author": "Dr. Ana Petrov, Cardiology",
                            "text": (
                                "Heart failure clinic follow-up. NYHA class II, weight stable at 80 kg. No orthopnea. "
                                "Potassium 5.4 mmol/L on spironolactone - dose reduced to 12.5 mg. Continue "
                                "sacubitril-valsartan. Referred for ICD evaluation if LVEF remains below 35% at 3 months."
                            ),
                        }
                    ],
                },
            ],
        },
        {
            "mrn": "0056789",
            "name": {"given": "Aisha", "family": "Rahman"},
            "dob": "05/09/1990",
            "sex": "female",
            "encounters": [
                {
                    "encounter_id": "ENC-5001",
                    "class": "ambulatory",
                    "start": "2026-06-03",
                    "documents": [
                        {
                            "doc_id": "D-5001-A",
                            # Missing type -> inferred from the text ("MRI").
                            "date": "06/03/2026",
                            "author": "Dr. Samuel Reyes, Radiology",
                            "text": (
                                "MRI brain with and without contrast. No acute infarct, mass or hemorrhage. No abnormal "
                                "enhancement. IMPRESSION: Normal MRI of the brain."
                            ),
                        },
                        {
                            "doc_id": "D-5001-B",
                            "type": "Progress Note",
                            "date": "2026-06-10",
                            "author": "Dr. Lena Fischer, Neurology",
                            "text": (
                                "Chronic migraine without aura, 16 headache days per month, medication overuse with "
                                "ibuprofen. Neurological exam normal; MRI brain normal. Started topiramate 25 mg nightly, "
                                "titrate to 50 mg. Counseled on limiting analgesics to fewer than 10 days per month."
                            ),
                        },
                        {
                            # Empty body -> dropped with an audit entry.
                            "doc_id": "D-5001-C",
                            "type": "Scanned Document",
                            "date": "2026-06-10",
                            "text": "   ",
                        },
                    ],
                }
            ],
        },
        {
            "mrn": "MRN-0067890",
            "name": {"given": "David", "family": "Kowalski"},
            "dob": "1966-09-17",
            "sex": "1",
            "encounters": [
                {
                    "encounter_id": "ENC-6001",
                    "class": "ambulatory",
                    "start": "2025-10-20",
                    "documents": [
                        {
                            "doc_id": "D-6001-A",
                            "type": "CT Chest",
                            "date": "10/20/2025",
                            "author": "Dr. Samuel Reyes, Radiology",
                            "text": (
                                "Low-dose CT chest for lung cancer screening. 30 pack-year smoker. Centrilobular emphysema, "
                                "upper lobe predominant. 9 mm solid nodule in the right upper lobe. Lung-RADS 4A. "
                                "IMPRESSION: Suspicious pulmonary nodule; recommend 3-month follow-up low-dose CT."
                            ),
                        },
                        {
                            "doc_id": "D-6001-B",
                            "type": "Progress Note",
                            "date": "2025-10-27",
                            "author": "Dr. Rafael Ortiz, Pulmonology",
                            "text": (
                                "COPD GOLD 2, FEV1 62% predicted. Continue tiotropium. Discussed RUL nodule found on "
                                "screening CT; plan repeat CT in 3 months versus PET-CT. Smoking cessation counseling, "
                                "started varenicline."
                            ),
                        },
                    ],
                },
                {
                    "encounter_id": "ENC-6002",
                    "class": "ambulatory",
                    "start": "2026-01-22",
                    "documents": [
                        {
                            "doc_id": "D-6002-A",
                            "type": "CT Chest",
                            "date": "01/22/2026",
                            "author": "Dr. Samuel Reyes, Radiology",
                            "text": (
                                "Follow-up CT chest. The right upper lobe nodule has grown from 9 mm to 13 mm with new "
                                "spiculated margins. Lung-RADS 4B. IMPRESSION: Interval growth, highly suspicious for "
                                "malignancy. Recommend PET-CT and tissue sampling."
                            ),
                        }
                    ],
                },
            ],
        },
        {
            "mrn": "0078901",
            "name": {"given": "Emily", "family": "Johnson"},
            "dob": "2001-12-03",
            "sex": "F",
            "encounters": [
                {
                    "encounter_id": "ENC-7001",
                    "class": "emergency",
                    "start": "2026-08-09",
                    "documents": [
                        {
                            "doc_id": "D-7001-A",
                            "type": "CT Abdomen Pelvis",
                            "date": "08/09/2026",
                            "author": "Dr. Samuel Reyes, Radiology",
                            "text": (
                                "CT abdomen and pelvis with IV contrast. Dilated appendix measuring 12 mm with "
                                "periappendiceal fat stranding and an appendicolith. No abscess or free air. "
                                "IMPRESSION: Acute appendicitis."
                            ),
                        },
                        {
                            "doc_id": "D-7001-B",
                            "type": "Operative Note",
                            "date": "2026-08-09",
                            "author": "Dr. Nina Alvarez, General Surgery",
                            "text": (
                                "Procedure: Laparoscopic appendectomy. Findings: inflamed, non-perforated appendix. "
                                "Estimated blood loss 10 mL. No complications. Patient tolerated the procedure well."
                            ),
                        },
                        {
                            "doc_id": "D-7001-C",
                            "type": "Discharge Summary",
                            # Impossible future date (typo for 2026) -> flagged and replaced from the encounter.
                            "date": "08/10/2062",
                            "author": "Dr. Nina Alvarez",
                            "text": (
                                "Uncomplicated laparoscopic appendectomy for acute appendicitis. Tolerating diet, pain "
                                "controlled with acetaminophen. Discharged postoperative day 1. Return precautions for fever "
                                "or worsening abdominal pain."
                            ),
                        },
                    ],
                }
            ],
        },
        {
            # Same person as MRN-0012345 exported a second time with different formatting.
            "mrn": "12345",
            "name": "GONZALEZ, MARIA",
            "dob": "1958-03-14",
            "sex": "f",
            "encounters": [],
        },
        {
            "mrn": "0099012",
            "name": {"given": "Grace", "family": "Park"},
            "dob": "08/08/1977",
            # Missing sex -> 'unknown' unless another source supplies it.
            "encounters": [
                {
                    "encounter_id": "ENC-9001",
                    "class": "ambulatory",
                    "start": "2026-04-14",
                    "documents": [
                        {
                            "doc_id": "D-9001-A",
                            "type": "Mammogram",
                            "date": "04/14/2026",
                            "author": "Dr. Yuki Tanaka, Breast Imaging",
                            "text": (
                                "Diagnostic mammogram, left breast. 1.8 cm irregular spiculated mass at 2 o'clock with "
                                "associated pleomorphic microcalcifications. BI-RADS 4C: high suspicion for malignancy. "
                                "Ultrasound-guided core biopsy recommended."
                            ),
                        },
                        {
                            "doc_id": "D-9001-B",
                            "type": "Breast Ultrasound",
                            "date": "2026-04-14",
                            "author": "Dr. Yuki Tanaka, Breast Imaging",
                            "text": (
                                "Targeted left breast ultrasound: 1.7 x 1.4 cm hypoechoic irregular mass with angular "
                                "margins at 2 o'clock, 4 cm from the nipple. One borderline left axillary lymph node with "
                                "cortical thickening of 3.5 mm."
                            ),
                        },
                    ],
                },
                {
                    "encounter_id": "ENC-9002",
                    "class": "ambulatory",
                    "start": "2026-04-21",
                    "documents": [
                        {
                            "doc_id": "D-9002-A",
                            "type": "Pathology Report",
                            "date": "2026-04-21",
                            "author": "Dr. Victor Hale, Pathology",
                            "text": (
                                "Left breast, 2 o'clock, core needle biopsy: invasive ductal carcinoma, Nottingham grade 2. "
                                "Estrogen receptor positive (95%), progesterone receptor positive (60%), HER2 negative "
                                "(IHC 1+). Ki-67 20%."
                            ),
                        }
                    ],
                },
            ],
        },
        {
            "mrn": "MRN-0101123",
            "name": {"given": "Thomas", "family": "Muller"},
            "dob": "02/14/1955",
            "sex": "M",
            "encounters": [
                {
                    "encounter_id": "ENC-10001",
                    "class": "emergency",
                    "start": "2026-09-02",
                    "documents": [
                        {
                            "doc_id": "D-10001-A",
                            "type": "CT Head",
                            "date": "2026-09-02T14:05:00",
                            "author": "Dr. Samuel Reyes, Radiology",
                            "text": (
                                "Non-contrast CT head. No intracranial hemorrhage. Subtle loss of gray-white differentiation "
                                "in the left insular cortex. IMPRESSION: Findings concerning for early left MCA territory "
                                "ischemic stroke."
                            ),
                        },
                        {
                            "doc_id": "D-10001-B",
                            "type": "MRI Brain",
                            "date": "09/03/2026",
                            "author": "Dr. Samuel Reyes, Radiology",
                            "text": (
                                "MRI brain: restricted diffusion in the left frontal operculum and insula consistent with "
                                "acute infarct. Moderate chronic small vessel ischemic disease. MRA: severe stenosis of "
                                "the left internal carotid artery origin."
                            ),
                        },
                        {
                            "doc_id": "D-10001-C",
                            "type": "Discharge Summary",
                            "date": "09/07/2026",
                            "author": "Dr. Lena Fischer, Neurology",
                            "text": (
                                "Acute left MCA ischemic stroke presenting with expressive aphasia and right arm weakness. "
                                "Received IV tenecteplase within 3 hours. NIHSS improved from 8 to 3. Left ICA stenosis 80% "
                                "on carotid duplex; vascular surgery planning carotid endarterectomy. Started aspirin, "
                                "clopidogrel for 21 days and high-intensity statin. Outpatient speech therapy."
                            ),
                        },
                    ],
                }
            ],
        },
    ],
}

# --------------------------------------------------------------------------------------
# 2. Lab results CSV (one row per analyte, grouped later into DiagnosticReports)
# --------------------------------------------------------------------------------------
LAB_HEADER = [
    "MRN", "Patient Name", "DOB", "Sex", "Accession", "Collected", "Panel", "Test", "LOINC",
    "Result", "Units", "Ref Range", "Flag",
]

LAB_ROWS = [
    # Maria Gonzalez - note the MRN written a third way
    ["12345", "Gonzalez, Maria", "3/14/1958", "F", "ACC-5001", "2026/02/10 06:15", "Basic Metabolic Panel", "Glucose", "2345-7", "412", "mg/dL", "70-99", "H"],
    ["12345", "Gonzalez, Maria", "3/14/1958", "F", "ACC-5001", "2026/02/10 06:15", "Basic Metabolic Panel", "Creatinine", "2160-0", "2.4", "mg/dL", "0.6-1.1", "HIGH"],
    ["12345", "Gonzalez, Maria", "3/14/1958", "F", "ACC-5001", "2026/02/10 06:15", "Basic Metabolic Panel", "Potassium", "2823-3", "5.1", "mmol/L", "3.5-5.1", ""],
    ["12345", "Gonzalez, Maria", "3/14/1958", "F", "ACC-5001", "2026/02/10 06:15", "Basic Metabolic Panel", "eGFR", "33914-3", "27", "mL/min/1.73m2", ">60", "L"],
    ["0012345", "Gonzalez, Maria", "3/14/1958", "F", "ACC-5002", "02-10-2026", "Hemoglobin A1c", "Hemoglobin A1c", "4548-4", "10.8", "%", "4.0-5.6", "H"],
    # Duplicate row (exact) -> deduplicated
    ["0012345", "Gonzalez, Maria", "3/14/1958", "F", "ACC-5002", "02-10-2026", "Hemoglobin A1c", "Hemoglobin A1c", "4548-4", "10.8", "%", "4.0-5.6", "H"],
    # James Okafor
    ["MRN-0023456", "Okafor, James", "11/02/1972", "M", "ACC-5101", "2026-05-18 07:50", "Cardiac Markers", "Troponin I, high sensitivity", "89579-7", "88", "ng/L", "<20", "H"],
    ["MRN-0023456", "Okafor, James", "11/02/1972", "M", "ACC-5102", "2026-05-18 10:55", "Cardiac Markers", "Troponin I, high sensitivity", "89579-7", "240", "ng/L", "<20", "CRIT"],
    ["MRN-0023456", "Okafor, James", "11/02/1972", "M", "ACC-5103", "2026-05-19 05:00", "Lipid Panel", "LDL Cholesterol", "13457-7", "168", "mg/dL", "<100", "H"],
    ["MRN-0023456", "Okafor, James", "11/02/1972", "M", "ACC-5103", "2026-05-19 05:00", "Lipid Panel", "HDL Cholesterol", "2085-9", "34", "mg/dL", ">40", "L"],
    # Linda Chen - name spelled differently ("Lynda") with the same MRN -> name variant audit
    ["34567", "Chen, Lynda", "07/21/1985", "F", "ACC-5201", "01/07/2026 09:10", "Complete Blood Count", "WBC", "6690-2", "11.2", "10*3/uL", "4.0-11.0", "H"],
    ["34567", "Chen, Lynda", "07/21/1985", "F", "ACC-5201", "01/07/2026 09:10", "Complete Blood Count", "Eosinophils", "26449-9", "8.5", "%", "0-6", "H"],
    ["34567", "Chen, Lynda", "07/21/1985", "F", "ACC-5201", "01/07/2026 09:10", "Complete Blood Count", "Hemoglobin", "718-7", "13.4", "g/dL", "12.0-15.5", ""],
    # Robert Williams under a *transposed* MRN (0045687 vs 0045678) -> conflicting identifier
    ["0045687", "WILLIAMS, ROBERT", "01/30/1949", "M", "ACC-5301", "2025-12-01 03:20", "Heart Failure Labs", "NT-proBNP", "33762-6", "6800", "pg/mL", "<450", "H"],
    ["0045687", "WILLIAMS, ROBERT", "01/30/1949", "M", "ACC-5301", "2025-12-01 03:20", "Heart Failure Labs", "Sodium", "2951-2", "131", "mmol/L", "135-145", "L"],
    ["0045678", "Williams, Robert", "01/30/1949", "M", "ACC-5302", "2026-01-15 08:00", "Basic Metabolic Panel", "Potassium", "2823-3", "5.4", "mmol/L", "3.5-5.1", "H"],
    ["0045678", "Williams, Robert", "01/30/1949", "M", "ACC-5302", "2026-01-15 08:00", "Basic Metabolic Panel", "Creatinine", "2160-0", "1.3", "mg/dL", "0.7-1.3", ""],
    # Emily Johnson
    ["0078901", "Johnson, Emily", "12/03/2001", "F", "ACC-5401", "08/09/2026 01:30", "Complete Blood Count", "WBC", "6690-2", "16.4", "10*3/uL", "4.0-11.0", "H"],
    ["0078901", "Johnson, Emily", "12/03/2001", "F", "ACC-5401", "08/09/2026 01:30", "Complete Blood Count", "Neutrophils", "770-8", "86", "%", "40-70", "H"],
    ["0078901", "Johnson, Emily", "12/03/2001", "F", "ACC-5402", "08/09/2026 01:30", "Pregnancy Test", "hCG, serum qualitative", "2106-3", "Negative", "", "Negative", ""],
    # Samuel Adeyemi - only appears in the labs (no JSON entry) -> patient created from lab demographics
    ["0089012", "Adeyemi, Samuel", "1980-04-25", "M", "ACC-5501", "2026-07-11 08:30", "Basic Metabolic Panel", "Potassium", "2823-3", "6.3", "mmol/L", "3.5-5.1", "CRIT"],
    ["0089012", "Adeyemi, Samuel", "1980-04-25", "M", "ACC-5501", "2026-07-11 08:30", "Basic Metabolic Panel", "Creatinine", "2160-0", "1.9", "mg/dL", "0.7-1.3", "H"],
    ["0089012", "Adeyemi, Samuel", "1980-04-25", "M", "ACC-5501", "2026-07-11 08:30", "Basic Metabolic Panel", "Sodium", "2951-2", "138", "mmol/L", "135-145", ""],
    # Grace Park - sex supplied here (missing in JSON)
    ["0099012", "Park, Grace", "08/08/1977", "F", "ACC-5601", "2026-04-21 11:00", "Tumor Markers", "CA 15-3", "6875-9", "38", "U/mL", "<30", "H"],
    # Thomas Muller
    ["MRN-0101123", "Muller, Thomas", "02/14/1955", "M", "ACC-5701", "2026-09-03 06:00", "Lipid Panel", "LDL Cholesterol", "13457-7", "142", "mg/dL", "<70", "H"],
    ["MRN-0101123", "Muller, Thomas", "02/14/1955", "M", "ACC-5701", "2026-09-03 06:00", "Lipid Panel", "Triglycerides", "2571-8", "210", "mg/dL", "<150", "H"],
    ["MRN-0101123", "Muller, Thomas", "02/14/1955", "M", "ACC-5702", "2026-09-03 06:00", "Hemoglobin A1c", "Hemoglobin A1c", "4548-4", "6.1", "%", "4.0-5.6", "H"],
    # Patricia Brown - only appears in labs + scanned notes
    ["0112234", "Brown, Patricia", "06/30/1962", "F", "ACC-5801", "2026-03-19 09:00", "Thyroid Panel", "TSH", "3016-3", "9.8", "mIU/L", "0.4-4.0", "H"],
    ["0112234", "Brown, Patricia", "06/30/1962", "F", "ACC-5801", "2026-03-19 09:00", "Thyroid Panel", "Free T4", "3024-7", "0.7", "ng/dL", "0.8-1.8", "L"],
    ["0112234", "Brown, Patricia", "06/30/1962", "F", "ACC-5802", "2026-03-19 09:00", "Vitamin D", "25-hydroxyvitamin D", "1989-3", "14", "ng/mL", "30-100", "L"],
    # Kevin Nguyen - ambiguous date "04/05/2026" (Apr 5 under US convention)
    ["0123345", "Nguyen, Kevin", "10/12/1995", "M", "ACC-5901", "04/05/2026", "Basic Metabolic Panel", "Creatinine", "2160-0", "1.0", "mg/dL", "0.7-1.3", ""],
    # Row with no MRN at all -> matched on name + DOB
    ["", "Nguyen, Kevin", "10/12/1995", "M", "ACC-5902", "04/05/2026", "Vitamin D", "25-hydroxyvitamin D", "1989-3", "41", "ng/mL", "30-100", ""],
]

# --------------------------------------------------------------------------------------
# 3. "Scanned" notes (plain text, OCR-like)
# --------------------------------------------------------------------------------------
SCANNED_NOTES = """\
==================== SCANNED DOCUMENT ====================
Patient: Brown, Patricia
MRN#: 112234
DOB: 1962-06-30
Sex: Female
Date of Service: 03/26/2026
Document Type: Progress Note
Author: Dr. Ruth Klein, Endocrinology
------------------------------------------------------------
Pt seen for fatigue, weight gain and cold intolerance.  TSH 9.8 with low free T4
consistent with primary hypo-
thyroidism; anti-TPO antibodies positive (Hashimoto thyroiditis).  Started levo-
thyroxine 75 mcg daily. Vitamin D deficiency (14 ng/mL) - started ergocalciferol
50,000 IU weekly x 8 weeks.  Recheck TSH in 6 weeks.

==================== SCANNED DOCUMENT ====================
Patient: Brown, Patricia
MRN#: 112234
DOB: 1962-06-30
Sex: Female
Date of Service: 04/02/2026
Document Type: DEXA Scan
Author: Dr. Yuki Tanaka, Radiology
------------------------------------------------------------
Bone densitometry.  Lumbar spine T-score -2.7, left femoral neck T-score -2.1.
IMPRESSION:  Osteoporosis of the lumbar spine.  FRAX 10-year major osteoporotic
fracture risk 18%.

==================== SCANNED DOCUMENT ====================
Patient: Nguyen, Kevin
MRN#: 0123345
DOB: 10/12/1995
Sex: M
Date of Service: 2026-04-05
Document Type: X-Ray Wrist
Author: Dr. Samuel Reyes, Radiology
------------------------------------------------------------
Three views of the right wrist after fall onto outstretched hand while snow-
boarding.  Non-displaced fracture of the scaphoid waist.  No dislocation.
IMPRESSION:  Acute non-displaced scaphoid fracture.

==================== SCANNED DOCUMENT ====================
Patient: Nguyen, Kevin
MRN#: 0123345
DOB: 10/12/1995
Sex: M
Date of Service: 2026-04-05
Document Type: Orthopedic Consult
Author: Dr. Paul Grant, Orthopedics
------------------------------------------------------------
Right scaphoid waist fracture, non-displaced.  Placed in thumb spica cast for
8-10 weeks.  Repeat X-ray in 2 weeks; consider CT or MRI if healing is delayed
given risk of avascular necrosis.  No smoking.

==================== SCANNED DOCUMENT ====================
Patient: Adeyemi, Samuel
MRN#: 89012
DOB: 04/25/1980
Sex: Male
Date of Service: 07/11/2026
Document Type: Progress Note
Author: Dr. Grace Obi, Internal Medicine
------------------------------------------------------------
Hypertension, poorly controlled (BP 168/102) on lisinopril 40 mg and
spironolactone 25 mg.  Labs today: potassium 6.3 mmol/L (critical) and
creatinine 1.9 mg/dL, up from 1.2 last year.  Spironolactone stopped,
lisinopril held, ECG without peaked T waves.  Sent to ED for repeat potassium
and treatment.  Renal artery duplex ordered to evaluate secondary hypertension.

==================== SCANNED DOCUMENT ====================
Patient: Adeyemi, Samuel
MRN#: 89012
DOB: 04/25/1980
Sex: Male
Date of Service: 07/18/2026
Document Type: Renal Artery Duplex
Author: Dr. Samuel Reyes, Radiology
------------------------------------------------------------
Renal artery duplex ultrasound.  Peak systolic velocity 310 cm/s at the
origin of the right renal artery, renal-aortic ratio 4.1.  IMPRESSION:
Findings consistent with greater than 60% right renal artery stenosis.

==================== SCANNED DOCUMENT ====================
Patient: Okafor, James
MRN#: 23456
DOB: 11/02/1972
Sex: M
Date of Service: 06/20/2026
Document Type: Progress Note
Author: Dr. Ana Petrov, Cardiology
------------------------------------------------------------
One month post NSTEMI with LAD stent.  No angina.  Tolerating aspirin and
ticagrelor.  Enrolled in cardiac rehabilitation.  LDL still 112 on atorva-
statin 80 mg - adding ezetimibe 10 mg.  Repeat echo in 3 months.

==================== SCANNED DOCUMENT ====================
Patient: Kowalski, David
MRN#: 67890
DOB: 09/17/1966
Sex: M
Date of Service: 02/05/2026
Document Type: PET-CT
Author: Dr. Samuel Reyes, Nuclear Medicine
------------------------------------------------------------
FDG PET-CT.  13 mm spiculated right upper lobe nodule is FDG avid with SUV max
7.2.  Mildly avid right hilar lymph node, SUV 3.1.  No distant metastatic
disease.  IMPRESSION:  Findings consistent with primary lung malignancy,
clinical stage at least T1c N1.

==================== SCANNED DOCUMENT ====================
Patient: Park, Grace
MRN#: 99012
DOB: 1977-08-08
Sex: F
Date of Service: 05/01/2026
Document Type: Consult Note
Author: Dr. Isabel Moreno, Medical Oncology
------------------------------------------------------------
New diagnosis of left breast invasive ductal carcinoma, ER/PR positive, HER2
negative, clinical stage IIA (cT1c N1).  Plan: breast MRI, then neoadjuvant
endocrine therapy versus upfront surgery per tumor board.  Genetic counseling
referral (mother with breast cancer at 48).

==================== SCANNED DOCUMENT ====================
Patient: Chen, Linda
MRN#: 0034567
DOB: 07/21/1985
Sex: F
Date of Service: 01/21/2026
Document Type: Progress Note
Author: Dr. Rafael Ortiz, Pulmonology
------------------------------------------------------------
Asthma follow-up two weeks after ED visit.  Symptoms controlled on ICS-
formoterol.  Spirometry FEV1 82% predicted with 14% bronchodilator response.
Blood eosinophils 8.5% - consider biologic therapy if exacerbations recur.

==================== SCANNED DOCUMENT ====================
Patient: Williams, Robert
MRN#: 45678
DOB: 01/30/1949
Sex: M
Date of Service: 2026-03-10
Document Type: Echocardiogram
Author: Dr. Ana Petrov, Cardiology
------------------------------------------------------------
Transthoracic echo on guideline-directed medical therapy.  LVEF 30%, severe
global hypokinesis.  Moderate functional mitral regurgitation.  Left atrium
severely dilated.  IMPRESSION:  Persistent severe LV systolic dysfunction;
meets criteria for primary prevention ICD.

==================== SCANNED DOCUMENT ====================
Patient: Muller, Thomas
MRN#: 101123
DOB: 02/14/1955
Sex: M
Date of Service: 09/20/2026
Document Type: Operative Note
Author: Dr. Hannah Weiss, Vascular Surgery
------------------------------------------------------------
Left carotid endarterectomy with patch angioplasty.  80% heterogeneous plaque
removed.  Uncomplicated.  Neurologically intact post-op.

==================== SCANNED DOCUMENT ====================
Patient: Gonzalez, Maria
MRN#: 12345
DOB: 03/14/1958
Sex: F
Date of Service: 2026-05-04
Document Type: Retinal Exam
Author: Dr. Leo Martin, Ophthalmology
------------------------------------------------------------
Dilated fundus examination.  Moderate non-proliferative diabetic retinopathy
in both eyes with scattered microaneurysms and dot-blot hemorrhages.  No
macular edema on OCT.  Repeat exam in 6 months.
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ehr_export.json").write_text(json.dumps(EHR_EXPORT, indent=2), encoding="utf-8")
    with (OUT / "lab_results.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(LAB_HEADER)
        writer.writerows(LAB_ROWS)
    (OUT / "scanned_notes.txt").write_text(SCANNED_NOTES, encoding="utf-8")
    print(f"Wrote synthetic data to {OUT}")


if __name__ == "__main__":
    main()
