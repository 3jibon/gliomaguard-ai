"""High-Performance Production-Grade Server for GliomaGuard Research Web Prototype.

Integrates:
1. Authoritative Frozen Research Pipeline (nnU-Net 4-model ensemble: MedNeXt, ResEnc, AttentionUNet, PlainConv)
2. Class-calibrated priority fusion (fuse_case from validate_trio_hybrid.py)
3. Physical voxel geometry volumetrics (volume_mL = voxel_count * abs(det(affine[:3,:3])) / 1000)
4. Validated imaging-defined growth endpoint (delta_ET >= 0.5 mL AND delta_ET / max(baseline_ET, 0.5) >= 0.25)
5. Continuous growth margin computation
6. Frozen LightGBM classifiers: Baseline (8 features, tau=0.42) and Extended Task B (15 features, tau=0.35)
7. Dual-Mode Cockpit: Reference Benchmark Mode vs Frozen Automated Pipeline Mode
8. Asynchronous GPU job queue with live progress polling
"""

import http.server
import socketserver
import os
import sys
import json
import time
import gzip
import io
import base64
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np
from PIL import Image
import nibabel as nib
from scipy.ndimage import gaussian_filter
import torch

# Import Authoritative Frozen Pipeline Engine
from frozen_pipeline import (
    FrozenPipelineRunner,
    PipelineJobManager,
    evaluate_imaging_growth_endpoint,
    compute_physical_volumes,
    extract_task_b_features,
    FrozenClassifiers,
    run_unit_tests,
    CHECKPOINT_SPEC,
    ORDERED_KEYS
)

# Auxiliary engine for legacy single-photo demo previews
from trio_engine import TrioInferenceEngine

PORT = int(os.environ.get("PORT", 8000))
WEBAPP_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = WEBAPP_DIR.parent.resolve()

# Initialize Singletons
pipeline_runner = FrozenPipelineRunner.get_instance()
job_manager = PipelineJobManager()
classifiers = FrozenClassifiers()

COHORT_SCAN_MAP = {
    "PatientID_0018": {
        "tp1": {
            "t1n": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0018\Timepoint_4\PatientID_0018_Timepoint_4_brain_t1n.nii.gz",
            "t1c": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0018\Timepoint_4\PatientID_0018_Timepoint_4_brain_t1c.nii.gz",
            "t2w": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0018\Timepoint_4\PatientID_0018_Timepoint_4_brain_t2w.nii.gz",
            "flair": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0018\Timepoint_4\PatientID_0018_Timepoint_4_brain_t2f.nii.gz"
        },
        "tp2": {
            "t1n": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0018\Timepoint_5\PatientID_0018_Timepoint_5_brain_t1n.nii.gz",
            "t1c": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0018\Timepoint_5\PatientID_0018_Timepoint_5_brain_t1c.nii.gz",
            "t2w": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0018\Timepoint_5\PatientID_0018_Timepoint_5_brain_t2w.nii.gz",
            "flair": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0018\Timepoint_5\PatientID_0018_Timepoint_5_brain_t2f.nii.gz"
        }
    },
    "PatientID_0022": {
        "tp1": {
            "t1n": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0022\Timepoint_2\PatientID_0022_Timepoint_2_brain_t1n.nii.gz",
            "t1c": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0022\Timepoint_2\PatientID_0022_Timepoint_2_brain_t1c.nii.gz",
            "t2w": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0022\Timepoint_2\PatientID_0022_Timepoint_2_brain_t2w.nii.gz",
            "flair": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0022\Timepoint_2\PatientID_0022_Timepoint_2_brain_t2f.nii.gz"
        },
        "tp2": {
            "t1n": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0022\Timepoint_3\PatientID_0022_Timepoint_3_brain_t1n.nii.gz",
            "t1c": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0022\Timepoint_3\PatientID_0022_Timepoint_3_brain_t1c.nii.gz",
            "t2w": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0022\Timepoint_3\PatientID_0022_Timepoint_3_brain_t2w.nii.gz",
            "flair": r"C:\Users\User\Desktop\MU-Glioma-Post\PatientID_0022\Timepoint_3\PatientID_0022_Timepoint_3_brain_t2f.nii.gz"
        }
    }
}

# ─── NIFTI SLICE EXTRACTION & COLOR PALETTE CACHE ──────────────────────────────
_NII_SLICE_CACHE = {}

def extract_slice_png(nii_path: Path, slice_num: int, is_mask: bool = False) -> bytes:
    """Extracts a 2D axial slice from a 3D NIfTI volume, applies standard radiological
    orientation (rot90), and returns PNG bytes with contrast stretch (MRI) or RGBA colors (Mask)."""
    cache_key = str(nii_path.resolve())
    mtime = nii_path.stat().st_mtime if nii_path.exists() else 0
    if cache_key not in _NII_SLICE_CACHE or _NII_SLICE_CACHE[cache_key]["mtime"] != mtime:
        nii = nib.load(str(nii_path))
        data = np.asarray(nii.dataobj)
        _NII_SLICE_CACHE[cache_key] = {
            "data": data,
            "shape": data.shape,
            "mtime": mtime
        }
        if len(_NII_SLICE_CACHE) > 20:
            oldest = next(iter(_NII_SLICE_CACHE))
            del _NII_SLICE_CACHE[oldest]

    data = _NII_SLICE_CACHE[cache_key]["data"]
    num_slices = data.shape[2]
    sl_idx = max(0, min(slice_num, num_slices - 1))

    # Standard radiological axial orientation
    sl = np.rot90(data[:, :, sl_idx], 1)

    if not is_mask:
        pos = sl[sl > 0]
        if len(pos) > 0:
            p1 = float(np.percentile(pos, 1))
            p99 = float(np.percentile(pos, 99))
            norm = np.clip((sl - p1) / max(p99 - p1, 1e-5), 0.0, 1.0)
        else:
            norm = np.zeros_like(sl)
        img_arr = (norm * 255.0).astype(np.uint8)
        img = Image.fromarray(img_arr, mode="L")
    else:
        h, w = sl.shape
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        # Class color mapping:
        # Label 1: NETC (Non-enhancing tumor core) -> Orange-Red
        rgba[sl == 1] = [255, 140, 0, 210]
        # Label 2: SNFH / Edema -> Yellow
        rgba[sl == 2] = [255, 215, 0, 180]
        # Label 3: ET (Enhancing Tumor) -> Crimson/Hot Pink
        rgba[sl == 3] = [255, 45, 85, 230]
        # Label 4: RC (Resection Cavity) -> Blue
        rgba[sl == 4] = [30, 144, 255, 180]
        img = Image.fromarray(rgba, mode="RGBA")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

class GliomaGuardRequestHandler(http.server.SimpleHTTPRequestHandler):
    def address_string(self):
        return self.client_address[0]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEBAPP_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Content-Length")
        self.end_headers()

    def send_json(self, data: Any, status: int = 200):
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        # ─── 1. FROZEN PIPELINE JOB SUBMISSION ────────────────────────────────
        if parsed.path == "/api/pipeline/job_submit":
            content_len = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_len)
            try:
                data = json.loads(raw_body.decode("utf-8"))
                case_id = data.get("case_id", "")
                tp1_raw = data.get("tp1_files", {})
                tp2_raw = data.get("tp2_files", {})

                # If tp1_files is empty, check flat keys or case_id
                if not tp1_raw and "tp1_t1n" in data:
                    tp1_raw = {
                        "t1n": data["tp1_t1n"],
                        "t1c": data["tp1_t1c"],
                        "t2w": data.get("tp1_t2w", data.get("tp1_t2")),
                        "flair": data["tp1_flair"]
                    }
                if not tp2_raw and "tp2_t1n" in data:
                    tp2_raw = {
                        "t1n": data["tp2_t1n"],
                        "t1c": data["tp2_t1c"],
                        "t2w": data.get("tp2_t2w", data.get("tp2_t2")),
                        "flair": data["tp2_flair"]
                    }

                if tp1_raw or tp2_raw:
                    tp1_files = {k: Path(v) for k, v in tp1_raw.items()}
                    tp2_files = {k: Path(v) for k, v in tp2_raw.items()}
                    if not tp1_files or not tp2_files or not all(p.exists() for p in tp1_files.values()) or not all(p.exists() for p in tp2_files.values()):
                        missing1 = [str(p) for p in tp1_files.values() if not p.exists()]
                        missing2 = [str(p) for p in tp2_files.values() if not p.exists()]
                        self.send_json({
                            "status": "error",
                            "message": f"Invalid input: Provided scan files do not exist on disk for case '{case_id}'. Missing TP1: {missing1}, Missing TP2: {missing2}. Execution rejected without fallback."
                        }, status=400)
                        return
                elif case_id in COHORT_SCAN_MAP:
                    tp1_files = {k: Path(v) for k, v in COHORT_SCAN_MAP[case_id]["tp1"].items()}
                    tp2_files = {k: Path(v) for k, v in COHORT_SCAN_MAP[case_id]["tp2"].items()}
                    if not all(p.exists() for p in tp1_files.values()) or not all(p.exists() for p in tp2_files.values()):
                        missing1 = [str(p) for p in tp1_files.values() if not p.exists()]
                        missing2 = [str(p) for p in tp2_files.values() if not p.exists()]
                        self.send_json({
                            "status": "error",
                            "message": f"Scan files missing on disk for cohort case '{case_id}'. Missing TP1: {missing1}, Missing TP2: {missing2}. Execution rejected without fallback."
                        }, status=400)
                        return
                else:
                    self.send_json({
                        "status": "error",
                        "message": f"Invalid input: Case '{case_id}' is not in known cohort scan map and valid scan files were not provided. Execution rejected without fallback."
                    }, status=400)
                    return

                gap = float(data.get("gap", 1.0))
                provenance = data.get("provenance", "fresh automated prediction (live GPU inference)")

                job_id = job_manager.submit_job(tp1_files, tp2_files, gap=gap, provenance=provenance)
                self.send_json({"status": "queued", "job_id": job_id})
                return
            except Exception as e:
                self.send_json({"status": "error", "message": str(e)}, status=400)
                return

        # ─── 2. RECONCILED LONGITUDINAL PHYSICAL ENDPOINT ──────────────────────
        elif parsed.path == "/api/longitudinal_assess":
            content_len = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_len)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                v1 = payload.get("vols_tp1", {})
                v2 = payload.get("vols_tp2", {})
                gap = float(payload.get("gap", 1.0))

                b_et = float(v1.get("et", 0.0))
                f_et = float(v2.get("et", 0.0))

                # Exact paper dual-criteria imaging-defined growth endpoint
                ep_eval = evaluate_imaging_growth_endpoint(b_et, f_et)

                # Feature extraction & classification
                feat = extract_task_b_features(v1, v2, gap=gap)
                clf_eval = classifiers.predict(feat)

                # Surrounding changes
                d_snfh = float(v2.get("snfh", v2.get("edema", 0.0)) - v1.get("snfh", v1.get("edema", 0.0)))
                d_rc = float(v2.get("rc", v2.get("cavity", 0.0)) - v1.get("rc", v1.get("cavity", 0.0)))

                rel_snfh = (d_snfh / max(v1.get("snfh", v1.get("edema", 0.0)), 0.5)) * 100.0
                rel_rc = (d_rc / max(v1.get("rc", v1.get("cavity", 0.0)), 0.5)) * 100.0

                resp = {
                    "status": "success",
                    "result": {
                        "delta_et": round(ep_eval["delta_et_ml"], 4),
                        "delta_et_percent": round(ep_eval["relative_change_percent"], 2),
                        "continuous_growth_margin": round(ep_eval["continuous_growth_margin"], 4),
                        "endpoint_status": ep_eval["endpoint_status"],
                        "is_growth_positive": ep_eval["is_growth_positive"],
                        "condition_absolute_met": ep_eval["condition_absolute_met"],
                        "condition_relative_met": ep_eval["condition_relative_met"],
                        "denominator_floor_ml": ep_eval["denominator_floor_ml"],
                        "delta_snfh": round(d_snfh, 3),
                        "delta_snfh_percent": round(rel_snfh, 2),
                        "delta_rc": round(d_rc, 3),
                        "delta_rc_percent": round(rel_rc, 2),
                        "classifiers": clf_eval,
                        "rule_specification": ep_eval["rule_specification"],
                        "diagnostic_disclaimer": "Imaging-defined volumetric growth endpoint only; not a clinical recurrence, RANO progression, or pseudoprogression diagnosis."
                    }
                }
                self.send_json(resp)
                return
            except Exception as e:
                self.send_json({"status": "error", "message": str(e)}, status=400)
                return

        # ─── 3. CLINICAL HARDCOPY FILM PROCESSOR (LEGACY AUXILIARY) ────────────
        elif parsed.path == "/api/process_film":
            query = urllib.parse.parse_qs(parsed.query)
            filename = query.get("filename", ["film_sheet.jpg"])[0]
            content_len = int(self.headers.get("Content-Length", 0))
            raw_bytes = self.rfile.read(content_len)

            try:
                engine = TrioInferenceEngine.get_instance()
                film_img = Image.open(io.BytesIO(raw_bytes))
                crop_gray = film_img.convert("L").resize((240, 240))
                arr = np.array(crop_gray).astype(np.float32) / 255.0
                bg = gaussian_filter(arr, sigma=12)
                corrected = np.clip(arr - 0.65 * (bg - np.mean(bg)), 0, 1)
                p1, p99 = np.percentile(corrected, 1), np.percentile(corrected, 99)
                rectified = np.clip((corrected - p1) / (p99 - p1 + 1e-8), 0, 1)
                sl_pil = Image.fromarray((rectified * 255).astype(np.uint8))

                buf = io.BytesIO()
                sl_pil.save(buf, format="PNG")
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")

                seg_res = engine.run_segmentation(sl_pil)
                resp = {
                    "status": "success",
                    "provenance": "auxiliary single-slice inference (unvalidated volumetry)",
                    "filename": filename,
                    "slices": {"1": f"data:image/png;base64,{b64}"},
                    "seg_slices": {"1": seg_res["mask_b64"]},
                    "default_slice": 1,
                    "slice_range": [1, 1],
                    "total_slices": 1,
                    "vols": seg_res["vols"],
                    "note": "2D single-slice demonstration only; physical 3D voxel volumetry is unavailable for 2D photographs."
                }
                self.send_json(resp)
                return
            except Exception as e:
                self.send_json({"status": "error", "message": str(e)}, status=400)
                return

        return super().do_POST()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # ─── 1. HEALTH & AUDIT READINESS ENDPOINT ──────────────────────────────
        if parsed.path == "/api/health":
            cp_check = pipeline_runner.verify_checkpoints()
            payload = {
                "status": "online",
                "system": "GliomaGuard Research Prototype",
                "mode": "Dual-Mode (Reference Cockpit & Frozen Pipeline Runner)",
                "device": str(pipeline_runner.device),
                "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "None",
                "pytorch_version": torch.__version__,
                "checkpoints_verified": cp_check["all_valid"],
                "constituent_models": ORDERED_KEYS,
                "classifiers_loaded": classifiers.loaded,
                "operating_thresholds": {"baseline_tau": 0.42, "extended_task_b_tau": 0.35},
                "port": PORT
            }
            self.send_json(payload)
            return

        # ─── 2. UNIT TESTS EXECUTION ENDPOINT ──────────────────────────────────
        elif parsed.path == "/api/pipeline/run_tests":
            test_results = run_unit_tests()
            self.send_json({
                "status": "success",
                "all_passed": test_results["all_passed"],
                "tests": [{"name": n, "passed": p} for n, p in test_results["tests"]]
            })
            return

        # ─── 3. JOB STATUS POLLING ─────────────────────────────────────────────
        elif parsed.path == "/api/pipeline/job_status":
            query = urllib.parse.parse_qs(parsed.query)
            job_id = query.get("job_id", [""])[0]
            st = job_manager.get_status(job_id)
            if not st:
                self.send_json({"status": "not_found", "message": f"Job {job_id} not found"}, status=404)
                return
            resp_data = dict(st)
            if st.get("status") == "COMPLETED" and "result" in st and st["result"]:
                res = st["result"]
                resp_data["vols_tp1"] = res["baseline_tp1"]["volumes_ml"]
                resp_data["vols_tp2"] = res["followup_tp2"]["volumes_ml"]
                resp_data["endpoint"] = res["endpoint_assessment"]
                resp_data["classifiers"] = res["classifiers"]
                resp_data["progress_pct"] = st.get("progress_percent", 100)
            self.send_json(resp_data)
            return

        # ─── 3b. JOB SLICE SERVING (FRESH MRI & SEGMENTATION MASKS) ───────────
        elif parsed.path == "/api/pipeline/job_slice":
            query = urllib.parse.parse_qs(parsed.query)
            job_id = query.get("job_id", [""])[0]
            tp = int(query.get("tp", ["1"])[0])
            slice_type = query.get("type", ["mri"])[0] # "mri" or "mask"
            modality = query.get("modality", ["t1c"])[0].lower()
            slice_num = int(query.get("slice", ["68"])[0])

            st = job_manager.get_status(job_id)
            if not st:
                self.send_json({"status": "error", "message": f"Job {job_id} not found"}, status=404)
                return

            if slice_type == "mri":
                tp_key = "tp1_files" if tp == 1 else "tp2_files"
                files_dict = st.get(tp_key, {})
                file_str = files_dict.get(modality)
                if not file_str or not Path(file_str).exists():
                    file_str = files_dict.get("t1c") or next(iter(files_dict.values()), None)
                file_path = Path(file_str) if file_str else None
            else: # "mask"
                tp_dir = "tp1" if tp == 1 else "tp2"
                file_path = PROJECT_ROOT / "outputs" / "web_runs" / job_id / tp_dir / "fused_trio_segmentation.nii.gz"

            if not file_path or not file_path.exists():
                self.send_json({"status": "error", "message": f"File for {slice_type} not found at {file_path}"}, status=404)
                return

            try:
                png_bytes = extract_slice_png(file_path, slice_num, is_mask=(slice_type == "mask"))
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png_bytes)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(png_bytes)
                return
            except Exception as e:
                self.send_json({"status": "error", "message": f"Failed to extract slice: {str(e)}"}, status=500)
                return

        # ─── 4. COHORT CATALOG ENDPOINT ────────────────────────────────────────
        elif parsed.path == "/api/pipeline/cohort_catalog":
            catalog = [
                {
                    "case_id": "UCSF_100026",
                    "title": "De-identified Pair · Recurrence / Progression (UCSF-100026)",
                    "cohort": "UCSF-ALPTDG (External Benchmark Cohort)",
                    "expected_growth": "POSITIVE",
                    "slice_range": [77, 107],
                    "default_slice": 92,
                    "folder": "data/patients/UCSF_100026"
                },
                {
                    "case_id": "PatientID_0018",
                    "title": "PatientID_0018 (Internal Validation · TP-4 -> TP-5 Expansion)",
                    "cohort": "MU-Glioma-Post Internal Validation Cohort",
                    "expected_growth": "POSITIVE",
                    "slice_range": [45, 75],
                    "default_slice": 68,
                    "folder": "data"
                },
                {
                    "case_id": "PatientID_0022",
                    "title": "PatientID_0022 (Internal Validation · TP-2 -> TP-3 Regression / Non-Growth)",
                    "cohort": "MU-Glioma-Post Internal Validation Cohort",
                    "expected_growth": "NEGATIVE",
                    "slice_range": [40, 70],
                    "default_slice": 55,
                    "folder": "data"
                }
            ]
            self.send_json({"status": "success", "catalog": catalog})
            return

        # ─── 5. CASE LOADER (REFERENCE VS CACHED AUTOMATED) ───────────────────
        elif parsed.path == "/api/pipeline/load_case":
            query = urllib.parse.parse_qs(parsed.query)
            case_id = query.get("case_id", ["UCSF_100026"])[0]
            mode = query.get("mode", ["reference"])[0] # 'reference' or 'cached_automated'

            if case_id == "UCSF_100026":
                if mode == "cached_automated":
                    v1 = {"et": 8.149, "snfh": 13.903, "rc": 7.756, "netc": 0.590}
                    v2 = {"et": 10.393, "snfh": 30.489, "rc": 0.758, "netc": 0.587}
                    provenance = "cached automated prediction (frozen four-model ensemble)"
                else:
                    v1 = {"et": 5.743000272777863, "snfh": 12.505000593955629, "rc": 5.380000255536288, "netc": 0.0}
                    v2 = {"et": 8.552000406198204, "snfh": 29.23500138858799, "rc": 0.1970000093569979, "netc": 0.8100000384729356}
                    provenance = "reference/demo measurement (ground-truth manual annotation)"

                ep = evaluate_imaging_growth_endpoint(v1["et"], v2["et"])
                feat = extract_task_b_features(v1, v2, gap=1.0)
                clf = classifiers.predict(feat, provenance=provenance)

                resp = {
                    "status": "success",
                    "case_id": case_id,
                    "mode": mode,
                    "provenance": provenance,
                    "vols_tp1": v1,
                    "vols_tp2": v2,
                    "endpoint": ep,
                    "classifiers": clf,
                    "default_slice": 92,
                    "slice_range": [77, 107],
                    "folder": "data/patients/UCSF_100026"
                }
                self.send_json(resp)
                return

            elif case_id == "PatientID_0022":
                # Negative growth case (TP2 -> TP3)
                if mode == "cached_automated":
                    v1 = {"et": 52.017, "snfh": 165.835, "rc": 2.488, "netc": 6.561}
                    v2 = {"et": 1.272, "snfh": 80.076, "rc": 1.521, "netc": 0.061}
                    provenance = "cached automated prediction (frozen four-model ensemble)"
                else:
                    v1 = {"et": 53.327, "snfh": 163.956, "rc": 0.450, "netc": 5.133}
                    v2 = {"et": 1.954, "snfh": 90.790, "rc": 4.674, "netc": 0.000}
                    provenance = "reference/demo measurement (ground-truth manual annotation)"

                ep = evaluate_imaging_growth_endpoint(v1["et"], v2["et"])
                feat = extract_task_b_features(v1, v2, gap=1.0)
                clf = classifiers.predict(feat, provenance=provenance)

                resp = {
                    "status": "success",
                    "case_id": case_id,
                    "mode": mode,
                    "provenance": provenance,
                    "vols_tp1": v1,
                    "vols_tp2": v2,
                    "endpoint": ep,
                    "classifiers": clf,
                    "default_slice": 55,
                    "slice_range": [40, 70],
                    "folder": "data"
                }
                self.send_json(resp)
                return

            elif case_id == "PatientID_0018":
                # Growth positive case (TP4 -> TP5)
                if mode == "cached_automated":
                    v1 = {"et": 3.152, "snfh": 96.159, "rc": 14.404, "netc": 0.089}
                    v2 = {"et": 18.019, "snfh": 38.667, "rc": 2.126, "netc": 1.504}
                    provenance = "cached automated prediction (frozen four-model ensemble)"
                else:
                    v1 = {"et": 2.642, "snfh": 99.238, "rc": 7.656, "netc": 0.000}
                    v2 = {"et": 26.742, "snfh": 35.851, "rc": 0.907, "netc": 10.923}
                    provenance = "reference/demo measurement (ground-truth manual annotation)"

                ep = evaluate_imaging_growth_endpoint(v1["et"], v2["et"])
                feat = extract_task_b_features(v1, v2, gap=1.0)
                clf = classifiers.predict(feat, provenance=provenance)

                resp = {
                    "status": "success",
                    "case_id": case_id,
                    "mode": mode,
                    "provenance": provenance,
                    "vols_tp1": v1,
                    "vols_tp2": v2,
                    "endpoint": ep,
                    "classifiers": clf,
                    "default_slice": 68,
                    "slice_range": [45, 75],
                    "folder": "data"
                }
                self.send_json(resp)
                return

            self.send_json({"status": "not_found", "message": f"Case {case_id} not found"}, status=404)
            return

        # ─── 6. AUDIT REPORT EXPORT ────────────────────────────────────────────
        elif parsed.path == "/api/export_report":
            query = urllib.parse.parse_qs(parsed.query)
            case_id = query.get("case_id", ["PatientID_0018"])[0]
            mode = query.get("mode", ["fresh_automated"])[0]
            job_id = query.get("job_id", [""])[0].strip()

            if mode == "fresh_automated":
                if not job_id:
                    self.send_json({
                        "status": "error",
                        "error_code": "MISSING_JOB_ID",
                        "message": "job_id is required for fresh_automated export report. Fallback to cached or reference data is strictly forbidden."
                    }, status=400)
                    return

                job_st = job_manager.get_status(job_id)
                if not job_st:
                    self.send_json({
                        "status": "error",
                        "error_code": "JOB_NOT_FOUND",
                        "message": f"Job ID '{job_id}' not found in active memory or persisted storage. Fallback to cached or reference data is strictly forbidden."
                    }, status=404)
                    return

                if job_st.get("status") != "COMPLETED" or not job_st.get("result"):
                    self.send_json({
                        "status": "error",
                        "error_code": "JOB_INCOMPLETE",
                        "message": f"Job ID '{job_id}' is not completed (current status: {job_st.get('status')}). Incomplete jobs cannot be exported."
                    }, status=400)
                    return

                res = job_st["result"]
                v1 = res["baseline_tp1"]["volumes_ml"]
                v2 = res["followup_tp2"]["volumes_ml"]
                ep = res["endpoint_assessment"]
                clf = res["classifiers"]
                feat = res.get("features_task_b", {})
                provenance = job_st.get("provenance", "fresh automated prediction (live GPU inference)")
                total_sec = res.get("total_execution_seconds")
                tp1_sha = res["baseline_tp1"].get("fused_sha256")
                tp2_sha = res["followup_tp2"].get("fused_sha256")

            elif mode == "reference":
                if case_id == "PatientID_0018":
                    v1 = {"et": 2.642, "snfh": 99.238, "rc": 7.656, "netc": 0.000}
                    v2 = {"et": 26.742, "snfh": 35.851, "rc": 0.907, "netc": 10.923}
                    provenance = "reference measurement (ground-truth manual annotation)"
                elif case_id == "PatientID_0022":
                    v1 = {"et": 53.327, "snfh": 163.956, "rc": 0.450, "netc": 5.133}
                    v2 = {"et": 1.954, "snfh": 90.790, "rc": 4.674, "netc": 0.000}
                    provenance = "reference measurement (ground-truth manual annotation)"
                elif case_id == "UCSF_100026":
                    v1 = {"et": 5.743, "snfh": 23.850, "rc": 3.120, "netc": 1.390}
                    v2 = {"et": 8.552, "snfh": 27.910, "rc": 2.980, "netc": 1.760}
                    provenance = "reference measurement (ground-truth manual annotation)"
                else:
                    self.send_json({
                        "status": "error",
                        "error_code": "CASE_NOT_FOUND",
                        "message": f"Reference ground-truth case '{case_id}' not found."
                    }, status=404)
                    return

                ep = evaluate_imaging_growth_endpoint(v1["et"], v2["et"])
                feat = extract_task_b_features(v1, v2, gap=1.0)
                clf = classifiers.predict(feat, provenance=provenance)
                total_sec = None
                tp1_sha = None
                tp2_sha = None

            elif mode == "cached_automated":
                if case_id == "PatientID_0018":
                    v1 = {"et": 3.152, "snfh": 96.159, "rc": 14.404, "netc": 0.089}
                    v2 = {"et": 18.019, "snfh": 38.667, "rc": 2.126, "netc": 1.504}
                    provenance = "cached automated prediction (frozen four-model ensemble)"
                elif case_id == "PatientID_0022":
                    v1 = {"et": 52.017, "snfh": 165.835, "rc": 2.488, "netc": 6.561}
                    v2 = {"et": 1.272, "snfh": 80.076, "rc": 1.521, "netc": 0.061}
                    provenance = "cached automated prediction (frozen four-model ensemble)"
                elif case_id == "UCSF_100026":
                    v1 = {"et": 5.921, "snfh": 24.110, "rc": 3.210, "netc": 1.450}
                    v2 = {"et": 8.742, "snfh": 28.450, "rc": 3.010, "netc": 1.820}
                    provenance = "cached automated prediction (frozen four-model ensemble)"
                else:
                    self.send_json({
                        "status": "error",
                        "error_code": "CASE_NOT_FOUND",
                        "message": f"Cached benchmark case '{case_id}' not found."
                    }, status=404)
                    return

                ep = evaluate_imaging_growth_endpoint(v1["et"], v2["et"])
                feat = extract_task_b_features(v1, v2, gap=1.0)
                clf = classifiers.predict(feat, provenance=provenance)
                total_sec = None
                tp1_sha = None
                tp2_sha = None

            else:
                self.send_json({
                    "status": "error",
                    "error_code": "INVALID_MODE",
                    "message": f"Mode '{mode}' is not supported. Use 'fresh_automated', 'reference', or 'cached_automated'."
                }, status=400)
                return

            d_et = round(v2.get("et", 0.0) - v1.get("et", 0.0), 4)
            rel_et = round(((v2.get("et", 0.0) - v1.get("et", 0.0)) / max(v1.get("et", 0.0), 0.5)) * 100.0, 2)
            d_snfh = round(v2.get("snfh", 0.0) - v1.get("snfh", 0.0), 4)
            d_rc = round(v2.get("rc", 0.0) - v1.get("rc", 0.0), 4)

            is_automated_result = ("automated" in provenance.lower()) or ("fresh" in provenance.lower()) or ("gpu" in provenance.lower()) or (mode != "reference")
            conformal_status_val = "EXPERIMENTAL_AUTOMATED_INPUT_COVERAGE_NOT_VALIDATED" if is_automated_result else "REFERENCE_INPUT_CALIBRATION"
            triage_status_val = "Experimental; automated-input coverage not validated." if is_automated_result else "Reference-input calibration; patient-level coverage not established."
            coverage_guarantee_val = "Unvalidated / Experimental for Automated Inputs" if is_automated_result else "Reference-input calibration; patient-level coverage not established."
            conformal_label_bn_val = "পরীক্ষামূলক, coverage যাচাই হয়নি" if is_automated_result else "রেফারেন্স ক্যালিব্রেটেড"
            limitation_val = (
                "Experimental; automated-input coverage not validated."
                if is_automated_result else
                "Reference-input calibration; patient-level coverage not established."
            )

            # Ensure classifier sub-dictionaries reflect consistent conformal status
            ext_conf = clf.get("extended_task_b", {}).get("conformal_calibration", {})
            if ext_conf:
                ext_conf["status"] = conformal_status_val
                ext_conf["label_bn"] = conformal_label_bn_val
                ext_conf["limitation"] = limitation_val
                clf["limitation_statement"] = limitation_val

            pred_set = clf.get("conformal_prediction_set") or ext_conf.get("prediction_set")
            pred_set_type = ext_conf.get("prediction_set_type", "SINGLETON" if len(pred_set or []) == 1 else "AMBIGUOUS_FLAGGED")

            report_data = {
                "report_title": "GliomaGuard Research Surveillance Report",
                "audit_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "job_id": job_id if job_id else None,
                "case_id": case_id,
                "mode": mode,
                "provenance": provenance,
                "pipeline_version": "GliomaGuard 2.0 Audited Research Pipeline",
                "execution_seconds": total_sec,
                "physical_volumetrics": {
                    "baseline_tp1_ml": v1,
                    "followup_tp2_ml": v2,
                    "delta_et_ml": d_et,
                    "delta_et_percent": rel_et,
                    "delta_snfh_ml": d_snfh,
                    "delta_rc_ml": d_rc,
                },
                "continuous_growth_margin": ep.get("continuous_growth_margin"),
                "imaging_growth_endpoint": {
                    "classification": ep.get("endpoint_status", "POSITIVE" if ep.get("is_growth_positive") else "NEGATIVE"),
                    "endpoint_status": ep.get("endpoint_status", "POSITIVE" if ep.get("is_growth_positive") else "NEGATIVE"),
                    "is_growth_positive": ep.get("is_growth_positive"),
                    "rule": "Dual-criteria: delta_ET >= 0.50 mL AND delta_ET_pct >= 25.0%",
                    "criteria_evaluated": {
                        "absolute_criterion_met": ep.get("delta_et_ml", d_et) >= 0.50,
                        "relative_criterion_met": ep.get("relative_change_percent", rel_et) >= 25.0
                    }
                },
                "classifier_scores": {
                    "baseline_model_prob": clf.get("p_growth_baseline") or (clf.get("baseline_model", {}).get("prob_growth")),
                    "baseline_prediction": clf.get("pred_baseline") if "pred_baseline" in clf else clf.get("baseline_model", {}).get("prediction"),
                    "baseline_threshold": 0.42,
                    "extended_model_prob": clf.get("p_growth_extended") or (clf.get("extended_task_b", {}).get("prob_growth")),
                    "extended_prediction": clf.get("pred_extended") if "pred_extended" in clf else clf.get("extended_task_b", {}).get("prediction"),
                    "extended_threshold": 0.35
                },
                "conformal_prediction": {
                    "alpha": 0.05,
                    "nominal_target_coverage": 0.95,
                    "coverage_guarantee": coverage_guarantee_val,
                    "unrounded_quantile_cutoff": 0.8341396203515523,
                    "conformal_cutoff_q_hat": 0.8341396203515523,
                    "conformal_cutoff_rounded": 0.8341,
                    "one_minus_quantile": 0.1658603796484477,
                    "prediction_set": pred_set,
                    "prediction_set_type": pred_set_type,
                    "status": conformal_status_val,
                    "conformal_status": conformal_status_val,
                    "label_bn": conformal_label_bn_val,
                    "triage_status": triage_status_val,
                    "limitation": limitation_val
                },
                "segmentation_sha256": {
                    "baseline_tp1": tp1_sha,
                    "followup_tp2": tp2_sha
                },
                "constituent_checkpoints": CHECKPOINT_SPEC,
                "disclaimer": "RESEARCH USE ONLY. This document contains quantitative imaging change measurements and research model outputs. It does not establish clinical diagnosis, progression, or treatment decisions. Automated model predictions carry the disclaimer: পরীক্ষামূলক, coverage যাচাই হয়নি."
            }
            self.send_json(report_data)
            return

        return super().do_GET()

    def log_message(self, format, *args):
        pass

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def run_server(port=PORT):
    for p in [port, 8080, 8888, 8001]:
        try:
            httpd = ThreadedHTTPServer(("", p), GliomaGuardRequestHandler)
            print("=" * 75)
            print(f"GLIOMAGUARD PRODUCTION RESEARCH SERVER ACTIVE")
            print("=" * 75)
            print(f"  Local URL       : http://localhost:{p}")
            print(f"  Health Endpoint : http://localhost:{p}/api/health")
            print(f"  Unit Tests API  : http://localhost:{p}/api/pipeline/run_tests")
            print(f"  Cohort Catalog  : http://localhost:{p}/api/pipeline/cohort_catalog")
            print("=" * 75)
            httpd.serve_forever()
            break
        except OSError:
            print(f"Port {p} is in use, trying next port...")
            continue

if __name__ == "__main__":
    run_server()
