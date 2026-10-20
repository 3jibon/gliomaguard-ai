#!/usr/bin/env python3
"""Lightweight Hosted Research Viewer Server for GliomaGuard.

Deployment Architecture:
- Render Cloud Service: Serves audited precomputed research results, paired MRI previews,
  predicted-mask overlays, endpoint derivations, classifier scores, and audit report exports.
- Local Runner: Dedicated host for frozen four-model fresh segmentation GPU inference.
- Zero Cloud Machine Learning Dependencies: Operates entirely using the Python standard library.
  No PyTorch, no nnU-Net, no LightGBM, and no local Windows filesystem paths.
"""

import http.server
import socketserver
import os
import sys
import json
import time
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional

PORT = int(os.environ.get("PORT", 8000))
WEBAPP_DIR = Path(__file__).resolve().parent
AUDITED_DIR = WEBAPP_DIR / "data" / "audited_runs"

# ─── DATASET ATTRIBUTION & COMPLIANCE ──────────────────────────────────────────
DATASET_ATTRIBUTION = {
    "primary_collection": {
        "name": "University of Missouri Post-operative Glioma Dataset (MU-Glioma-Post)",
        "license": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "doi": "10.7937/7K9K-3C83",
        "citation": "Yaseen, D., Garrett, F., Gass, J., Greaser, J., Isufi, E., Layfield, L. J., Nada, A., Porgorzelski, K., Sinclair, J., Tahon, N. H. M., & Thacker, J. (2025). University of Missouri Post-operative Glioma Dataset (MU-Glioma-Post) (Version 1) [Data set]. The Cancer Imaging Archive. https://doi.org/10.7937/7K9K-3C83",
        "policy": "TCIA Data Usage Policy: Scientific research and educational use permitted. Re-identification strictly prohibited."
    },
    "external_benchmark_collection": {
        "name": "University of California San Francisco Adult Longitudinal Post-Treatment Diffuse Glioma (UCSF-ALPTDG)",
        "license": "UCSF Center for Intelligent Imaging (Ci2) Data Use Agreement (Strictly Non-Commercial Research)",
        "doi": "10.1148/ryai.230182",
        "citation": "Fields, B. K. K., et al. (2024). The University of California San Francisco Adult Longitudinal Post-Treatment Diffuse Glioma MRI Dataset. Radiology: Artificial Intelligence, e230182.",
        "policy": "Non-commercial scientific research only. Unauthorized raw data redistribution prohibited."
    }
}

# ─── LOAD AUDITED RESEARCH PACKAGES ────────────────────────────────────────────
PACKAGED_JOBS: Dict[str, Any] = {}
MANIFEST: Dict[str, Any] = {}

def load_audited_packages():
    global MANIFEST, PACKAGED_JOBS
    manifest_file = AUDITED_DIR / "manifest.json"
    if manifest_file.exists():
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                MANIFEST = json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to load manifest.json: {e}")

    for jid in ["98c33b66", "b903d506"]:
        st_file = AUDITED_DIR / jid / "job_state.json"
        if st_file.exists():
            try:
                with open(st_file, "r", encoding="utf-8") as f:
                    PACKAGED_JOBS[jid] = json.load(f)
            except Exception as e:
                print(f"[WARN] Failed to load job_state for {jid}: {e}")

load_audited_packages()

# Authoritative reference measurements for ground truth comparison
REFERENCE_CASES = {
    "PatientID_0018": {
        "case_id": "PatientID_0018",
        "pair": "Timepoint_4 -> Timepoint_5",
        "provenance": "reference measurement (ground-truth manual annotation)",
        "vols_tp1": {"et": 2.642, "snfh": 99.238, "rc": 7.656, "netc": 0.000},
        "vols_tp2": {"et": 26.742, "snfh": 35.851, "rc": 0.907, "netc": 10.923},
        "endpoint": {
            "delta_et_ml": 24.100,
            "relative_change_percent": 912.188,
            "endpoint_status": "POSITIVE",
            "is_growth_positive": True,
            "continuous_growth_margin": 36.487
        },
        "classifiers": {
            "baseline": {"prob_growth": 0.812, "classification": "GROWTH_PREDICTED"},
            "extended_task_b": {
                "prob_growth": 0.978,
                "classification": "GROWTH_PREDICTED",
                "conformal_calibration": {
                    "cutoff_q_hat": 0.8341,
                    "prediction_set": ["GROWTH_PREDICTED"],
                    "status": "REFERENCE_INPUT_CALIBRATION",
                    "uncertainty_statement": "Reference-input calibration; patient-level coverage not established."
                }
            }
        },
        "default_slice": 68,
        "slice_range": [45, 75],
        "folder": "data"
    },
    "PatientID_0022": {
        "case_id": "PatientID_0022",
        "pair": "Timepoint_2 -> Timepoint_3",
        "provenance": "reference measurement (ground-truth manual annotation)",
        "vols_tp1": {"et": 53.327, "snfh": 163.956, "rc": 0.450, "netc": 5.133},
        "vols_tp2": {"et": 1.954, "snfh": 90.790, "rc": 4.674, "netc": 0.000},
        "endpoint": {
            "delta_et_ml": -51.373,
            "relative_change_percent": -96.336,
            "endpoint_status": "NEGATIVE",
            "is_growth_positive": False,
            "continuous_growth_margin": -102.746
        },
        "classifiers": {
            "baseline": {"prob_growth": 0.584, "classification": "GROWTH_PREDICTED"},
            "extended_task_b": {
                "prob_growth": 0.124,
                "classification": "NON_GROWTH",
                "conformal_calibration": {
                    "cutoff_q_hat": 0.8341,
                    "prediction_set": ["NON_GROWTH"],
                    "status": "REFERENCE_INPUT_CALIBRATION",
                    "uncertainty_statement": "Reference-input calibration; patient-level coverage not established."
                }
            }
        },
        "default_slice": 55,
        "slice_range": [40, 70],
        "folder": "data"
    }
}

class GliomaGuardCloudHandler(http.server.SimpleHTTPRequestHandler):
    """Production HTTP Handler for lightweight hosted viewer on Render."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEBAPP_DIR), **kwargs)

    def address_string(self):
        return self.client_address[0]

    def send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # ─── 1. HEALTH ENDPOINT ────────────────────────────────────────────────
        if path == "/api/health":
            self.send_json({
                "status": "online",
                "system": "GliomaGuard Research Viewer",
                "version": "2.0.1-Audited-Viewer-HeaderFixed",
                "mode": "hosted_research_viewer",
                "inference_engine": "Precomputed Audited Research Pipeline",
                "fresh_inference_status": "disabled_on_cloud_host",
                "fresh_inference_notice": "Hosted research viewer — precomputed automated results; fresh segmentation inference is not available on this service.",
                "port": PORT,
                "uncertainty_spec": {
                    "reference_uncertainty": "Reference-input calibration; patient-level coverage not established.",
                    "automated_uncertainty": "Experimental; automated-input coverage not validated."
                },
                "dataset_attribution": DATASET_ATTRIBUTION,
                "audited_jobs_available": list(PACKAGED_JOBS.keys())
            })
            return

        # ─── 2. COHORT CATALOG ─────────────────────────────────────────────────
        elif path == "/api/pipeline/cohort_catalog":
            catalog = [
                {
                    "case_id": "PatientID_0018",
                    "title": "PatientID_0018 (Audited Positive Growth · TP-4 -> TP-5 Expansion)",
                    "cohort": "MU-Glioma-Post (TCIA CC BY 4.0)",
                    "expected_growth": "POSITIVE",
                    "slice_range": [45, 75],
                    "default_slice": 68,
                    "audited_job_id": "98c33b66",
                    "folder": "data"
                },
                {
                    "case_id": "PatientID_0022",
                    "title": "PatientID_0022 (Audited Negative / Non-Growth · TP-2 -> TP-3 Regression)",
                    "cohort": "MU-Glioma-Post (TCIA CC BY 4.0)",
                    "expected_growth": "NEGATIVE",
                    "slice_range": [40, 70],
                    "default_slice": 55,
                    "audited_job_id": "b903d506",
                    "folder": "data"
                }
            ]
            self.send_json({"status": "success", "catalog": catalog})
            return

        # ─── 3. LOAD CASE ENDPOINT ─────────────────────────────────────────────
        elif path == "/api/pipeline/load_case":
            query = urllib.parse.parse_qs(parsed.query)
            case_id = query.get("case_id", [""])[0].strip()
            mode = query.get("mode", ["reference"])[0].strip()

            if not case_id:
                self.send_json({
                    "status": "error",
                    "error_code": "MISSING_CASE_ID",
                    "message": "case_id query parameter is required. Fallback to another patient is strictly prohibited."
                }, status=400)
                return

            if case_id not in ["PatientID_0018", "PatientID_0022"]:
                self.send_json({
                    "status": "error",
                    "error_code": "CASE_NOT_FOUND",
                    "message": f"Case '{case_id}' is not in the audited catalog. Fallback to another patient is strictly prohibited."
                }, status=404)
                return

            if mode == "cached_automated" or mode == "fresh_automated":
                job_id = "98c33b66" if case_id == "PatientID_0018" else "b903d506"
                job_st = PACKAGED_JOBS.get(job_id)
                if not job_st:
                    self.send_json({
                        "status": "error",
                        "error_code": "JOB_NOT_FOUND",
                        "message": f"Audited job for {case_id} not loaded."
                    }, status=404)
                    return

                res = job_st["result"]
                v1 = res["baseline_tp1"]["volumes_ml"]
                v2 = res["followup_tp2"]["volumes_ml"]
                ep = res["endpoint_assessment"]
                clf = res["classifiers"]

                # Ensure exact uncertainty text
                if "extended_task_b" in clf and "conformal_calibration" in clf["extended_task_b"]:
                    clf["extended_task_b"]["conformal_calibration"]["uncertainty_statement"] = "Experimental; automated-input coverage not validated."
                    clf["extended_task_b"]["conformal_calibration"]["label_bn"] = "পরীক্ষামূলক, coverage যাচাই হয়নি"
                clf["limitation_statement"] = "Experimental; automated-input coverage not validated."

                self.send_json({
                    "status": "success",
                    "case_id": case_id,
                    "mode": "cached_automated",
                    "job_id": job_id,
                    "provenance": "precomputed automated prediction (audited frozen four-model ensemble)",
                    "vols_tp1": v1,
                    "vols_tp2": v2,
                    "endpoint": ep,
                    "classifiers": clf,
                    "default_slice": 68 if case_id == "PatientID_0018" else 55,
                    "slice_range": [45, 75] if case_id == "PatientID_0018" else [40, 70],
                    "folder": "data"
                })
                return

            else:  # reference mode
                ref = REFERENCE_CASES.get(case_id)
                self.send_json({
                    "status": "success",
                    "case_id": case_id,
                    "mode": "reference",
                    "provenance": ref["provenance"],
                    "vols_tp1": ref["vols_tp1"],
                    "vols_tp2": ref["vols_tp2"],
                    "endpoint": ref["endpoint"],
                    "classifiers": ref["classifiers"],
                    "default_slice": ref["default_slice"],
                    "slice_range": ref["slice_range"],
                    "folder": ref["folder"]
                })
                return

        # ─── 4. JOB SLICE RETRIEVAL (PRECOMPUTED PNGS) ─────────────────────────
        elif path == "/api/pipeline/job_slice":
            query = urllib.parse.parse_qs(parsed.query)
            job_id = query.get("job_id", [""])[0].strip()
            tp = query.get("tp", ["1"])[0].strip()
            slice_type = query.get("type", ["mri"])[0].strip() # "mri" or "mask"
            modality = query.get("modality", ["t1c"])[0].strip().lower()
            slice_num = query.get("slice", ["68"])[0].strip()

            if job_id not in PACKAGED_JOBS:
                self.send_json({"status": "error", "message": f"Audited job '{job_id}' not found."}, status=404)
                return

            tp_dir = "tp1" if tp == "1" else "tp2"
            if slice_type == "mask":
                png_name = f"mask_{slice_num}.png"
            else:
                png_name = f"{modality}_{slice_num}.png"

            img_path = AUDITED_DIR / job_id / tp_dir / png_name
            if not img_path.exists():
                self.send_json({"status": "error", "message": f"Slice file '{png_name}' not found for job '{job_id}'."}, status=404)
                return

            try:
                with open(img_path, "rb") as f:
                    png_bytes = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png_bytes)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(png_bytes)
                return
            except Exception as e:
                self.send_json({"status": "error", "message": str(e)}, status=500)
                return

        # ─── 5. JOB STATUS ENDPOINT ────────────────────────────────────────────
        elif path == "/api/pipeline/job_status":
            query = urllib.parse.parse_qs(parsed.query)
            job_id = query.get("job_id", [""])[0].strip()

            if job_id in PACKAGED_JOBS:
                self.send_json(PACKAGED_JOBS[job_id])
                return
            else:
                self.send_json({"status": "error", "message": f"Job ID '{job_id}' not found in audited repository."}, status=404)
                return

        # ─── 6. AUDIT REPORT EXPORT ────────────────────────────────────────────
        elif path == "/api/export_report":
            query = urllib.parse.parse_qs(parsed.query)
            case_id = query.get("case_id", ["PatientID_0018"])[0].strip()
            mode = query.get("mode", ["cached_automated"])[0].strip()
            job_id = query.get("job_id", [""])[0].strip()

            if mode in ["fresh_automated", "cached_automated"]:
                if not job_id:
                    job_id = "98c33b66" if case_id == "PatientID_0018" else ("b903d506" if case_id == "PatientID_0022" else "")

                if not job_id or job_id not in PACKAGED_JOBS:
                    self.send_json({
                        "status": "error",
                        "error_code": "JOB_NOT_FOUND",
                        "message": f"Job ID '{job_id}' not found in audited records. Fallback to cached or reference data is strictly forbidden."
                    }, status=404)
                    return

                job_st = PACKAGED_JOBS[job_id]
                res = job_st["result"]
                v1 = res["baseline_tp1"]["volumes_ml"]
                v2 = res["followup_tp2"]["volumes_ml"]
                ep = res["endpoint_assessment"]
                clf = res["classifiers"]
                feat = res.get("features_task_b", {})
                provenance = job_st.get("provenance", "precomputed automated prediction (audited frozen four-model ensemble)")
                total_sec = res.get("total_execution_seconds")
                tp1_sha = res["baseline_tp1"].get("fused_sha256")
                tp2_sha = res["followup_tp2"].get("fused_sha256")
                uncertainty_statement = "Experimental; automated-input coverage not validated."

            elif mode == "reference":
                if case_id not in REFERENCE_CASES:
                    self.send_json({
                        "status": "error",
                        "error_code": "CASE_NOT_FOUND",
                        "message": f"Reference case '{case_id}' not found."
                    }, status=404)
                    return

                ref = REFERENCE_CASES[case_id]
                v1 = ref["vols_tp1"]
                v2 = ref["vols_tp2"]
                ep = ref["endpoint"]
                clf = ref["classifiers"]
                feat = {}
                provenance = ref["provenance"]
                total_sec = None
                tp1_sha = None
                tp2_sha = None
                uncertainty_statement = "Reference-input calibration; patient-level coverage not established."

            else:
                self.send_json({
                    "status": "error",
                    "error_code": "INVALID_MODE",
                    "message": f"Mode '{mode}' not supported."
                }, status=400)
                return

            d_et = round(v2.get("et", 0.0) - v1.get("et", 0.0), 4)
            rel_et = round(((v2.get("et", 0.0) - v1.get("et", 0.0)) / max(v1.get("et", 0.0), 0.5)) * 100.0, 2)
            d_snfh = round(v2.get("snfh", 0.0) - v1.get("snfh", 0.0), 4)
            d_rc = round(v2.get("rc", 0.0) - v1.get("rc", 0.0), 4)

            # Build HTML Audit Report
            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>GliomaGuard Surveillance Report - {case_id}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f1f5f9; padding: 24px; }}
    .report-card {{ max-width: 800px; margin: 0 auto; background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 24px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    h1 {{ color: #38bdf8; font-size: 1.5rem; margin-top: 0; }}
    .badge {{ display: inline-block; padding: 4px 10px; border-radius: 6px; font-size: 0.75rem; font-weight: 700; }}
    .badge.pos {{ background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; }}
    .badge.neg {{ background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid #22c55e; }}
    table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
    th, td {{ border: 1px solid #334155; padding: 8px 12px; text-align: left; font-size: 0.85rem; }}
    th {{ background: #0f172a; color: #94a3b8; }}
    .notice-box {{ background: rgba(59, 130, 246, 0.1); border-left: 4px solid #3b82f6; padding: 12px; margin: 16px 0; font-size: 0.82rem; }}
    .warn-box {{ background: rgba(245, 158, 11, 0.1); border-left: 4px solid #f59e0b; padding: 12px; margin: 16px 0; font-size: 0.82rem; }}
  </style>
</head>
<body>
  <div class="report-card">
    <h1>GliomaGuard Research Surveillance Report</h1>
    <p><strong>Case ID:</strong> {case_id} | <strong>Mode:</strong> {mode} {f'| <strong>Job ID:</strong> {job_id}' if job_id else ''}</p>
    <p><strong>Provenance:</strong> {provenance}</p>
    {f'<p><strong>Execution Time:</strong> {total_sec:.2f}s (Audited Local GPU Run)</p>' if total_sec else ''}

    <div class="notice-box">
      <strong>Hosted Research Viewer Notice:</strong> This report displays precomputed, audited research results. Fresh segmentation inference is not available on this cloud host.
    </div>

    <h3>1. Longitudinal Physical Volumetrics</h3>
    <table>
      <tr><th>Modality</th><th>Baseline TP-1 (mL)</th><th>Follow-Up TP-2 (mL)</th><th>Delta (mL)</th><th>Relative Change (%)</th></tr>
      <tr><td>Enhancing Tumor (ET)</td><td>{v1.get('et', 0.0):.3f}</td><td>{v2.get('et', 0.0):.3f}</td><td>{d_et:+.3f}</td><td>{rel_et:+.1f}%</td></tr>
      <tr><td>FLAIR Edema (SNFH)</td><td>{v1.get('snfh', 0.0):.3f}</td><td>{v2.get('snfh', 0.0):.3f}</td><td>{d_snfh:+.3f}</td><td>—</td></tr>
      <tr><td>Resection Cavity (RC)</td><td>{v1.get('rc', 0.0):.3f}</td><td>{v2.get('rc', 0.0):.3f}</td><td>{d_rc:+.3f}</td><td>—</td></tr>
    </table>

    <h3>2. Dual-Criteria Imaging Growth Endpoint</h3>
    <p>Status: <span class="badge {'pos' if ep.get('is_growth_positive') else 'neg'}">{ep.get('endpoint_status', 'UNKNOWN')}</span></p>
    <p>Continuous Margin: <strong>{ep.get('continuous_growth_margin', 0.0):.4f}</strong></p>
    <p>Rule: ΔET ≥ 0.5 mL AND ΔET / max(Baseline_ET, 0.5 mL) ≥ 25.0%</p>

    <h3>3. LightGBM Machine Learning Triage</h3>
    <p>Extended Task B Model Probability: <strong>{clf.get('extended_task_b', {}).get('prob_growth', 0.0):.3f}</strong> (Decision Threshold τ = 0.35)</p>
    
    <div class="warn-box">
      <strong>Uncertainty & Conformal Calibration:</strong><br>
      {uncertainty_statement}
    </div>

    {f'''<h3>4. Artifact Integrity Hashes (SHA-256)</h3>
    <p style="font-family: monospace; font-size: 0.75rem;">TP1 Fused Mask: {tp1_sha}<br>TP2 Fused Mask: {tp2_sha}</p>''' if tp1_sha else ''}

    <h3>5. Dataset Attribution & License</h3>
    <p style="font-size: 0.78rem; color: #94a3b8;">
      Dataset: {DATASET_ATTRIBUTION['primary_collection']['name']}<br>
      License: {DATASET_ATTRIBUTION['primary_collection']['license']} ({DATASET_ATTRIBUTION['primary_collection']['doi']})<br>
      Citation: {DATASET_ATTRIBUTION['primary_collection']['citation']}
    </p>
  </div>
</body>
</html>"""

            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Default static file handler
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # ─── 1. JOB SUBMISSION (CLEAR UNAVAILABLE NOTICE) ───────────────────────
        if path == "/api/pipeline/job_submit":
            self.send_json({
                "status": "unavailable",
                "error_code": "CLOUD_INFERENCE_DISABLED",
                "message": "Hosted research viewer — precomputed automated results; fresh segmentation inference is not available on this service. Please use the local GPU runner for fresh model execution."
            }, status=403)
            return

        # ─── 2. RECONCILED LONGITUDINAL PHYSICAL ENDPOINT ──────────────────────
        elif path == "/api/longitudinal_assess":
            content_len = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_len)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                v1 = payload.get("vols_tp1", {})
                v2 = payload.get("vols_tp2", {})

                b_et = float(v1.get("et", 0.0))
                f_et = float(v2.get("et", 0.0))
                d_et = f_et - b_et
                denom = max(b_et, 0.5)
                rel_change = (d_et / denom) * 100.0

                cond_abs = (d_et >= 0.5)
                cond_rel = (d_et / denom) >= 0.25
                is_pos = cond_abs and cond_rel

                margin_abs = d_et / 0.5
                margin_rel = d_et / (0.25 * denom)
                cont_margin = min(margin_abs, margin_rel)

                self.send_json({
                    "status": "success",
                    "delta_et_ml": d_et,
                    "relative_change_percent": rel_change,
                    "denominator_floor_ml": denom,
                    "condition_absolute_met": cond_abs,
                    "condition_relative_met": cond_rel,
                    "endpoint_status": "POSITIVE" if is_pos else "NEGATIVE",
                    "is_growth_positive": is_pos,
                    "continuous_growth_margin": cont_margin,
                    "rule_specification": "delta_ET >= 0.5 mL AND delta_ET / max(baseline_ET, 0.5 mL) >= 25.0%"
                })
                return
            except Exception as e:
                self.send_json({"status": "error", "message": str(e)}, status=400)
                return

        # ─── 3. DISABLED UPLOAD ROUTES ─────────────────────────────────────────
        elif path in ["/api/upload_nifti", "/api/process_film"]:
            self.send_json({
                "status": "unavailable",
                "error_code": "CLOUD_UPLOAD_DISABLED",
                "message": "File upload and live inference are disabled on this hosted research viewer. Use precomputed audited cases or run locally with GPU."
            }, status=400)
            return

        self.send_json({"status": "error", "message": f"Endpoint '{path}' not found"}, status=404)

def run():
    socketserver.TCPServer.allow_reuse_address = True
    server_address = ("", PORT)
    with socketserver.TCPServer(server_address, GliomaGuardCloudHandler) as httpd:
        print(f"================================================================")
        print(f"  GliomaGuard Lightweight Hosted Research Viewer Server")
        print(f"  Listening on http://0.0.0.0:{PORT}")
        print(f"  Mode: hosted_research_viewer (zero cloud ML dependencies)")
        print(f"  Audited packages loaded: {list(PACKAGED_JOBS.keys())}")
        print(f"================================================================")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")

if __name__ == "__main__":
    run()
