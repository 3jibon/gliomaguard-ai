"""GliomaGuard -- Authoritative Frozen Research Pipeline Runner & Volumetry Engine.

Connects the web prototype to the genuine audited research pipeline:
1. Four-model class-priority ensemble (DL-5 MedNeXt, DL-2 ResEnc, DL-3 AttentionUNet, DL-1 PlainConvUNet)
2. Class-calibrated priority fusion (fuse_case from validate_trio_hybrid.py)
3. Physical voxel-spacing volumetry: volume_mL = voxel_count * abs(det(affine[:3,:3])) / 1000
4. Exact paper dual-criteria endpoint: delta_ET >= 0.5 mL AND delta_ET / max(baseline_ET, 0.5 mL) >= 0.25
5. Continuous growth margin: min(delta_ET / 0.5, delta_ET / (0.25 * max(baseline_ET, 0.5)))
6. Frozen LightGBM classifiers: Baseline (8 features, tau=0.42) and Extended Task B (15 features, tau=0.35)
7. Multi-provenance tracking: 'fresh automated prediction', 'cached automated prediction', 'reference/demo measurement'
"""

import os
import sys
import time
import json
import uuid
import queue
import pickle
import hashlib
import shutil
import threading
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

import numpy as np
import nibabel as nib
import torch

# Ensure nnunet_laptop environment or site-packages are accessible if needed
PROJECT_ROOT = Path(__file__).resolve().parent.parent
NNUNET_VENV_PACKAGES = PROJECT_ROOT / "nnunet_laptop" / "venv" / "Lib" / "site-packages"
if NNUNET_VENV_PACKAGES.exists() and str(NNUNET_VENV_PACKAGES) not in sys.path:
    sys.path.insert(0, str(NNUNET_VENV_PACKAGES))

# Set mandatory nnU-Net environment variables
os.environ["nnUNet_raw"] = str(PROJECT_ROOT / "nnunet_laptop" / "raw")
os.environ["nnUNet_preprocessed"] = str(PROJECT_ROOT / "nnunet_laptop" / "preprocessed")
os.environ["nnUNet_results"] = str(PROJECT_ROOT / "nnunet_laptop" / "results")
os.environ["nnUNet_compile"] = "false"
os.environ["nnUNet_n_proc_DA"] = "1"

# ─── 1. AUTHORITATIVE CHECKPOINTS & FUSION SPECIFICATION ─────────────────────
CHECKPOINT_SPEC = {
    "DL-5_MedNeXt_ConvNeXt": {
        "rel_path": "nnunet_laptop/results/Dataset901_MUPostFrozen/nnUNetTrainerMedNeXtCompare300__nnUNetPlansLaptop2D__2d/best_within30_evaluation_e1ad58b0e0bb/checkpoint_frozen.pth",
        "sha256": "e1ad58b0e0bb2a7bc92bfd0ce1f0ffae8e59188486287d1fac81c847c4eb63b5",
        "trainer": "nnUNetTrainerMedNeXtCompare300",
        "architecture": "MedNeXt 2D (ConvNeXt blocks)"
    },
    "DL-2_ResidualEncoderUNet": {
        "rel_path": "nnunet_laptop/results/Dataset901_MUPostFrozen/nnUNetTrainerResEncCompare300__nnUNetPlansLaptop2D__2d/best_within30_evaluation_ecabd79ae499/checkpoint_frozen.pth",
        "sha256": "ecabd79ae4992bce74b02ad45e669da33186c7a68e3934bc593f982c78eb270e",
        "trainer": "nnUNetTrainerResEncCompare300",
        "architecture": "Residual Encoder UNet 2D"
    },
    "DL-3_AttentionUNet_CABA": {
        "rel_path": "nnunet_laptop/results/Dataset901_MUPostFrozen/nnUNetTrainerAttentionUNetCompare300__nnUNetPlansLaptop2D__2d/best_within30_evaluation_54843db06c80/checkpoint_frozen.pth",
        "sha256": "54843db06c80ae4aa91366b18dd45591e8be66562f6ee767d806d07ea909543b",
        "trainer": "nnUNetTrainerAttentionUNetCompare300",
        "architecture": "Attention UNet with Cavity-Aware Boundary Attention"
    },
    "DL-1_PlainConvUNet": {
        "rel_path": "nnunet_laptop/results/Dataset901_MUPostFrozen/nnUNetTrainerPlainConvCompare300__nnUNetPlansLaptop2D__2d/best_within30_evaluation_7df04f00cc29/checkpoint_frozen.pth",
        "sha256": "7df04f00cc2989bc8a1a346a8c0434d9ad227a49756018ca748bd75964c5cd9e",
        "trainer": "nnUNetTrainerPlainConvCompare300",
        "architecture": "Plain Convolutional UNet 2D (nnU-Net standard)"
    }
}

ORDERED_KEYS = [
    "DL-5_MedNeXt_ConvNeXt",
    "DL-2_ResidualEncoderUNet",
    "DL-3_AttentionUNet_CABA",
    "DL-1_PlainConvUNet"
]

MODELS_DIR = Path(__file__).resolve().parent / "models"

# ─── 2. ORDERED CLASS-PRIORITY FUSION (validate_trio_hybrid.py) ───────────────
def fuse_case(m_med: np.ndarray, m_res: np.ndarray, m_att: np.ndarray, m_plc: np.ndarray) -> np.ndarray:
    """Audited class-calibrated priority fusion on integer label arrays.
    
    1: NETC (Non-Enhancing Tumor Core)
    2: SNFH (Surrounding Non-Enhancing FLAIR Hyperintensity / Edema)
    3: ET (Enhancing Tumor)
    4: RC (Resection Cavity)
    """
    fused = np.zeros_like(m_med, dtype=np.uint8)

    # 1. FLAIR Infiltrative Edema (SNFH = 2)
    snfh_consensus = ((m_med == 2).astype(int) + (m_res == 2).astype(int) + (m_att == 2).astype(int)) >= 2
    fused[snfh_consensus | (m_med == 2)] = 2

    # 2. Resection Cavity (RC = 4)
    rc_consensus = ((m_res == 4).astype(int) + (m_med == 4).astype(int) + (m_plc == 4).astype(int)) >= 2
    fused[rc_consensus | (m_res == 4)] = 4

    # 3. Non-Enhancing Necrotic Core (NETC = 1)
    netc_agreement = ((m_res == 1).astype(int) + (m_plc == 1).astype(int) + (m_att == 1).astype(int)) >= 2
    fused[netc_agreement] = 1

    # 4. Enhancing Tumor Rim (ET = 3)
    et_confirmed = (m_med == 3) & ((m_res == 3) | (m_att == 3) | (m_plc == 3))
    et_isolated = (m_med == 3) & (fused != 4)
    fused[et_confirmed | et_isolated] = 3

    return fused

# ─── 3. PHYSICAL VOLUMETRICS & ENDPOINT MATH ──────────────────────────────────
def compute_physical_volumes(mask: np.ndarray, affine: np.ndarray, xyzt_units: Optional[Tuple[str, str]] = None) -> Dict[str, Any]:
    """Computes exact physical volumes in mL using voxel geometry:
    volume_mL = voxel_count * abs(det(affine[:3,:3])) * unit_factor / 1000.0
    """
    unit_factor = 1.0
    spatial_unit = "mm"
    if xyzt_units is not None:
        spatial_unit = str(xyzt_units[0]).lower()
        if spatial_unit in ("mm", "millimeter"):
            unit_factor = 1.0
        elif spatial_unit in ("meter", "m"):
            unit_factor = 1e9
        elif spatial_unit in ("micron", "um"):
            unit_factor = 1e-9
        elif spatial_unit in ("unknown", "none", ""):
            unit_factor = 1.0
        else:
            raise ValueError(f"Unsupported spatial unit in NIfTI header: '{spatial_unit}'")

    # Verify affine integrity
    det = float(np.linalg.det(affine[:3, :3]))
    voxel_vol_mm3 = float(abs(det) * unit_factor)
    if voxel_vol_mm3 < 1e-4 or np.isnan(voxel_vol_mm3) or np.isinf(voxel_vol_mm3):
        raise ValueError(f"Invalid affine determinant for physical volumetry: {det}")
    voxel_ml = voxel_vol_mm3 / 1000.0

    counts = {
        "netc": int((mask == 1).sum()),
        "snfh": int((mask == 2).sum()),
        "et":   int((mask == 3).sum()),
        "rc":   int((mask == 4).sum()),
    }
    volumes_ml = {k: float(counts[k] * voxel_ml) for k in counts}

    return {
        "voxel_counts": counts,
        "volumes_ml": volumes_ml,
        "voxel_volume_mm3": voxel_vol_mm3,
        "voxel_volume_ml": voxel_ml,
        "spatial_units": spatial_unit,
        "unit_factor": unit_factor
    }

def evaluate_imaging_growth_endpoint(baseline_et: float, followup_et: float) -> Dict[str, Any]:
    """Evaluates the exact paper dual-criteria imaging-defined growth endpoint:
    Condition 1: delta_ET >= 0.5 mL
    Condition 2: delta_ET / max(baseline_ET, 0.5 mL) >= 0.25 (25.0%)
    
    Continuous margin:
    min(delta_ET / 0.5, delta_ET / (0.25 * max(baseline_ET, 0.5)))
    """
    d_et = float(followup_et - baseline_et)
    denom = float(max(baseline_et, 0.5))
    rel_change_frac = float(d_et / denom)
    rel_change_pct = float(rel_change_frac * 100.0)

    cond_abs = bool(d_et >= 0.5)
    cond_rel = bool(rel_change_frac >= 0.25)
    is_growth_positive = bool(cond_abs and cond_rel)

    term1 = d_et / 0.5
    term2 = d_et / (0.25 * denom)
    continuous_margin = float(min(term1, term2))

    return {
        "delta_et_ml": d_et,
        "relative_change_percent": rel_change_pct,
        "denominator_floor_ml": denom,
        "condition_absolute_met": cond_abs,
        "condition_relative_met": cond_rel,
        "endpoint_status": "POSITIVE" if is_growth_positive else "NEGATIVE",
        "is_growth_positive": is_growth_positive,
        "continuous_growth_margin": continuous_margin,
        "rule_specification": "delta_ET >= 0.5 mL AND delta_ET / max(baseline_ET, 0.5 mL) >= 25.0%"
    }

# ─── 4. 15-FEATURE EXTRACTION & LIGHTGBM EVALUATION ───────────────────────────
def extract_task_b_features(v_base: Dict[str, float], v_follow: Dict[str, float], gap: float = 1.0) -> Dict[str, float]:
    """Extracts exact 15 Task B features matching audited Notebook Cell 2.4 and delta_radiomics_dataset_leakage_free.csv.
    
    Audited formulas:
    - v = max(et_base + rc_base, 0.01) * 1000.0
    - compaction_proxy_base = clip((pi^(1/3)) * ((6v)^(2/3)) / (4.836 * v^(2/3) + 10.0 * snfh_base), 0.05, 1.0)
    - ratio_et_edema_base = et_base / max(snfh_base, 0.5) [floor is 0.5 mL]
    - ratio_et_cavity_base = et_base / max(rc_base, 0.5) [floor is 0.5 mL]
    - delta_snfh = snfh_follow - snfh_base
    - rel_snfh = delta_snfh / max(snfh_base, 1.0) [floor is 1.0 mL]
    - vel_snfh = delta_snfh / max(gap, 1.0)
    - delta_rc = rc_follow - rc_base, vel_rc = delta_rc / max(gap, 1.0)
    - delta_netc = netc_follow - netc_base, vel_netc = delta_netc / max(gap, 1.0)
    """
    et_b = float(v_base.get("et", 0.0))
    snfh_b = float(v_base.get("snfh", 0.0))
    rc_b = float(v_base.get("rc", 0.0))
    netc_b = float(v_base.get("netc", 0.0))

    et_f = float(v_follow.get("et", 0.0))
    snfh_f = float(v_follow.get("snfh", 0.0))
    rc_f = float(v_follow.get("rc", 0.0))
    netc_f = float(v_follow.get("netc", 0.0))

    gap_val = float(max(gap, 1.0))

    # Audited Cell 2.4 formulas
    v = max(et_b + rc_b, 0.01) * 1000.0
    compaction_proxy = float(np.clip((np.pi**(1.0/3.0)) * ((6.0 * v)**(2.0/3.0)) / (4.836 * (v**(2.0/3.0)) + 10.0 * snfh_b), 0.05, 1.0))
    ratio_et_edema = float(et_b / max(snfh_b, 0.5))
    ratio_et_cavity = float(et_b / max(rc_b, 0.5))

    delta_snfh = float(snfh_f - snfh_b)
    rel_snfh = float(delta_snfh / max(snfh_b, 1.0))
    vel_snfh = float(delta_snfh / gap_val)

    delta_rc = float(rc_f - rc_b)
    vel_rc = float(delta_rc / gap_val)

    delta_netc = float(netc_f - netc_b)
    vel_netc = float(delta_netc / gap_val)

    feat = {
        "gap": gap_val,
        "et_base": et_b,
        "snfh_base": snfh_b,
        "rc_base": rc_b,
        "netc_base": netc_b,
        "compaction_proxy_base": compaction_proxy,
        "ratio_et_edema_base": ratio_et_edema,
        "ratio_et_cavity_base": ratio_et_cavity,
        "delta_snfh": delta_snfh,
        "rel_snfh": rel_snfh,
        "vel_snfh": vel_snfh,
        "delta_rc": delta_rc,
        "vel_rc": vel_rc,
        "delta_netc": delta_netc,
        "vel_netc": vel_netc,
    }
    return feat

class FrozenClassifiers:
    """Loads and executes baseline (tau=0.42) and extended (tau=0.35) LightGBM models,
    with conformal calibration bounds evaluated under alpha=0.05 (nominal 95% coverage)."""
    def __init__(self, models_dir: Path = MODELS_DIR):
        self.models_dir = Path(models_dir)
        self.base_model = None
        self.base_scaler = None
        self.ext_model = None
        self.ext_scaler = None
        self.loaded = False
        self._load()

    def _load(self):
        try:
            with open(self.models_dir / "baseline_lgbm_model.pkl", "rb") as f:
                self.base_model = pickle.load(f)
            with open(self.models_dir / "baseline_lgbm_scaler.pkl", "rb") as f:
                self.base_scaler = pickle.load(f)
            with open(self.models_dir / "extended_lgbm_model.pkl", "rb") as f:
                self.ext_model = pickle.load(f)
            with open(self.models_dir / "extended_lgbm_scaler.pkl", "rb") as f:
                self.ext_scaler = pickle.load(f)
            self.loaded = True
        except Exception as e:
            print(f"[FrozenClassifiers] Note: Could not load LightGBM models: {e}")

    def predict(self, features: Dict[str, float], provenance: str = "reference") -> Dict[str, Any]:
        if not self.loaded:
            return {"status": "unavailable", "message": "Classifiers not loaded"}

        base_cols = [
            "gap", "et_base", "snfh_base", "rc_base", "netc_base",
            "compaction_proxy_base", "ratio_et_edema_base", "ratio_et_cavity_base"
        ]
        ext_cols = base_cols + [
            "delta_snfh", "rel_snfh", "vel_snfh", "delta_rc", "vel_rc", "delta_netc", "vel_netc"
        ]

        x_b = np.array([[features[k] for k in base_cols]])
        x_e = np.array([[features[k] for k in ext_cols]])

        x_b_s = self.base_scaler.transform(x_b)
        x_e_s = self.ext_scaler.transform(x_e)

        prob_base = float(self.base_model.predict_proba(x_b_s)[0, 1])
        prob_ext = float(self.ext_model.predict_proba(x_e_s)[0, 1])

        tau_base = 0.42
        tau_ext = 0.35

        # Audited Conformal Calibration Parameters:
        # Full preserved precision: k=79 from 82 calibration pairs, alpha=0.05
        q_hat = 0.8341396203515523
        threshold_lower = float(1.0 - q_hat) # 0.1658603796484477
        threshold_upper = float(q_hat)       # 0.8341396203515523

        conformal_labels = []
        if (1.0 - prob_ext) >= threshold_lower: # i.e. prob_ext <= threshold_upper
            conformal_labels.append("NON_GROWTH")
        if prob_ext >= threshold_lower:
            conformal_labels.append("GROWTH_PREDICTED")

        conformal_set_type = "SINGLETON" if len(conformal_labels) == 1 else ("AMBIGUOUS_FLAGGED" if len(conformal_labels) == 2 else "EMPTY")

        is_automated = ("automated" in provenance.lower()) or ("fresh" in provenance.lower()) or ("gpu" in provenance.lower())
        conformal_status = "EXPERIMENTAL_AUTOMATED_INPUT_COVERAGE_NOT_VALIDATED" if is_automated else "VALIDATED_REFERENCE_DISTRIBUTION"
        conformal_label_bn = "পরীক্ষামূলক, coverage যাচাই হয়নি" if is_automated else "রেফারেন্স ক্যালিব্রেটেড (৯৫% কভারেজ)"
        conformal_limitation = (
            "Automated input conformal triage: Experimental research indicator only; coverage is not mathematically guaranteed (nominal 95% coverage calibrated on expert manual annotations only under exchangeability)."
            if is_automated else
            "Conformal coverage guarantees hold marginally under exchangeability of expert annotations only; unconditional coverage guarantees cannot be assumed."
        )

        base_dict = {
            "features_used": len(base_cols),
            "probability": round(prob_base, 4),
            "prob_growth": round(prob_base, 4),
            "operating_threshold": tau_base,
            "threshold": tau_base,
            "prediction": int(prob_base >= tau_base),
            "prediction_positive": bool(prob_base >= tau_base),
            "classification": "GROWTH_PREDICTED" if prob_base >= tau_base else "NON_GROWTH"
        }

        ext_dict = {
            "features_used": len(ext_cols),
            "probability": round(prob_ext, 4),
            "prob_growth": round(prob_ext, 4),
            "operating_threshold": tau_ext,
            "threshold": tau_ext,
            "prediction": int(prob_ext >= tau_ext),
            "prediction_positive": bool(prob_ext >= tau_ext),
            "classification": "GROWTH_PREDICTED" if prob_ext >= tau_ext else "NON_GROWTH",
            "conformal_calibration": {
                "alpha": 0.05,
                "target_nominal_coverage": 0.95,
                "conformal_cutoff_q_hat": q_hat,
                "conformal_cutoff_rounded": round(q_hat, 4),
                "threshold_lower": threshold_lower,
                "threshold_upper": threshold_upper,
                "prediction_set": conformal_labels,
                "prediction_set_type": conformal_set_type,
                "status": conformal_status,
                "label_bn": conformal_label_bn,
                "validation_empirical_coverage": 0.9744,
                "validation_single_label_efficiency": 0.4487,
                "validation_ambiguous_rate": 0.5513,
                "limitation": conformal_limitation
            }
        }

        return {
            "status": "success",
            "baseline": base_dict,
            "baseline_model": base_dict,
            "extended_task_b": ext_dict,
            "limitation_statement": conformal_limitation
        }

# ─── 5. FULL PIPELINE RUNNER (Serialized GPU Inference) ───────────────────────
class FrozenPipelineRunner:
    """Manages frozen four-model ensemble inference on pairs of multi-modal MRI scans."""
    _instance = None
    _gpu_lock = threading.Lock()

    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = Path(root)
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.classifiers = FrozenClassifiers()
        self.predictors = {}
        self.is_initialized = False
        self._checkpoint_report = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def verify_checkpoints(self) -> Dict[str, Any]:
        """Checks hashes and readiness of all 4 constituent checkpoints (cached after first check)."""
        if self._checkpoint_report is not None:
            return self._checkpoint_report

        report = {}
        all_ok = True
        for key, info in CHECKPOINT_SPEC.items():
            cp_path = self.root / info["rel_path"]
            exists = cp_path.exists()
            h_match = False
            sha = ""
            if exists:
                sha = hashlib.sha256(cp_path.read_bytes()).hexdigest()
                h_match = (sha == info["sha256"])
            report[key] = {
                "exists": exists,
                "hash_match": h_match,
                "sha256": sha,
                "expected_sha256": info["sha256"],
                "architecture": info["architecture"],
                "path": str(cp_path)
            }
            if not (exists and h_match):
                all_ok = False
        self._checkpoint_report = {"all_valid": all_ok, "checkpoints": report}
        return self._checkpoint_report

    def validate_modalities(self, mod_files: Dict[str, Path]) -> Tuple[bool, str, Dict[str, Any]]:
        """Verifies that T1n, T1c, T2w, and FLAIR exist, have matching shape,
        verified spatial units ('mm'), pairwise affine co-registration, and non-singular affine."""
        required = ["t1n", "t1c", "t2w", "flair"]
        for m in required:
            if m not in mod_files or not Path(mod_files[m]).exists():
                return False, f"Missing required sequence file: '{m}'", {}

        shapes = {}
        affines = {}
        units = {}
        for m in required:
            try:
                img = nib.load(str(mod_files[m]))
                shapes[m] = list(img.shape[:3])
                affines[m] = np.asarray(img.affine, dtype=np.float64)
                units[m] = img.header.get_xyzt_units()
            except Exception as e:
                return False, f"Failed reading NIfTI header for {m}: {e}", {}

        # 1. Check shape equality across all 4 sequences
        primary_shape = shapes["t1c"]
        for m, s in shapes.items():
            if s != primary_shape:
                return False, f"Shape mismatch: {m} has shape {s}, expected {primary_shape}", {}

        # 2. Check spatial units (must be 'mm')
        for m, u in units.items():
            spatial_unit = str(u[0]).lower()
            if spatial_unit not in ("mm", "millimeter"):
                return False, f"Invalid spatial unit for sequence '{m}': '{spatial_unit}'. Expected 'mm'.", {}

        # 3. Check pairwise affine co-registration to T1c (tolerance 0.01 mm)
        ref_affine = affines["t1c"]
        for m, aff in affines.items():
            if not np.allclose(aff, ref_affine, atol=1e-2):
                max_diff = float(np.max(np.abs(aff - ref_affine)))
                return False, f"Pairwise affine misalignment between '{m}' and 't1c': max diff = {max_diff:.4f} > 0.01", {}

        # 4. Check affine determinant
        det = float(np.linalg.det(ref_affine[:3, :3]))
        if abs(det) < 1e-4 or np.isnan(det) or np.isinf(det):
            return False, f"Invalid affine determinant ({det}) for reference sequence", {}

        return True, "Valid", {
            "shape": primary_shape,
            "spatial_units": "mm",
            "voxel_volume_mm3": float(abs(det)),
            "affine": ref_affine.tolist()
        }

    def predict_scan(self, input_paths: List[Path], output_dir: Path, tag: str, progress_cb=None) -> Tuple[Dict[str, Any], np.ndarray]:
        """Runs the four frozen models and fuses their predictions."""
        from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        arrays = {}
        timings = {}

        # Staging folder for model configurations
        work_models = output_dir / "work_models"
        work_models.mkdir(parents=True, exist_ok=True)

        with self._gpu_lock:
            for idx, key in enumerate(ORDERED_KEYS):
                if progress_cb:
                    progress_cb(f"Running {key} ({idx+1}/4)...", idx * 25)

                cp_info = CHECKPOINT_SPEC[key]
                cp_path = self.root / cp_info["rel_path"]
                plans_path = cp_path.parent.parent / "plans.json"
                dataset_path = cp_path.parent.parent / "dataset.json"

                staging = work_models / key
                (staging / "fold_0").mkdir(parents=True, exist_ok=True)
                for f_name, src in [("plans.json", plans_path), ("dataset.json", dataset_path), ("checkpoint_frozen.pth", cp_path)]:
                    dst = staging / ("fold_0/checkpoint_frozen.pth" if f_name == "checkpoint_frozen.pth" else f_name)
                    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                        shutil.copy2(src, dst)

                t0 = time.perf_counter()
                predictor = nnUNetPredictor(
                    tile_step_size=0.5,
                    use_gaussian=True,
                    use_mirroring=True,
                    perform_everything_on_device=True if self.device.type == "cuda" else False,
                    device=self.device,
                    verbose=False,
                    verbose_preprocessing=False,
                    allow_tqdm=False
                )
                predictor.initialize_from_trained_model_folder(
                    str(staging),
                    use_folds=(0,),
                    checkpoint_name="checkpoint_frozen.pth"
                )

                reader = predictor.plans_manager.image_reader_writer_class()
                img_data, props = reader.read_images([str(p) for p in input_paths])
                target_out = output_dir / f"{key}_pred"

                predictor.predict_single_npy_array(
                    img_data,
                    props,
                    output_file_truncated=str(target_out),
                    save_or_return_probabilities=False
                )
                pred_file = Path(str(target_out) + ".nii.gz")
                pred_nib = nib.load(pred_file)
                arrays[key] = np.asarray(pred_nib.dataobj, dtype=np.uint8)
                timings[key] = round(time.perf_counter() - t0, 2)

                # Free VRAM immediately
                predictor.network.cpu()
                del predictor
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

        if progress_cb:
            progress_cb("Fusing class-priority segmentations...", 90)

        # Execute class-calibrated priority fusion
        fused = fuse_case(
            arrays["DL-5_MedNeXt_ConvNeXt"],
            arrays["DL-2_ResidualEncoderUNet"],
            arrays["DL-3_AttentionUNet_CABA"],
            arrays["DL-1_PlainConvUNet"]
        )

        # Save fused NIfTI
        fused_path = output_dir / "fused_trio_segmentation.nii.gz"
        ref_nii = nib.load(str(input_paths[1])) # T1c affine
        fused_nii = nib.Nifti1Image(fused, ref_nii.affine, ref_nii.header)
        nib.save(fused_nii, fused_path)

        # Physical volumetry with explicit header spatial units
        vol_info = compute_physical_volumes(fused, ref_nii.affine, xyzt_units=ref_nii.header.get_xyzt_units())

        res = {
            "fused_path": str(fused_path),
            "fused_sha256": hashlib.sha256(fused_path.read_bytes()).hexdigest(),
            "timings_sec": timings,
            "total_inference_sec": round(sum(timings.values()), 2),
            "volumes_ml": vol_info["volumes_ml"],
            "voxel_counts": vol_info["voxel_counts"],
            "shape": list(fused.shape)
        }
        return res, fused

# ─── 6. ASYNCHRONOUS JOB QUEUE & STATE MANAGER ────────────────────────────────
class PipelineJobManager:
    """Handles background execution and polling for multi-minute frozen pipeline runs."""
    def __init__(self):
        self.jobs = {}
        self.queue = queue.Queue()
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    def submit_job(self, tp1_files: Dict[str, Path], tp2_files: Dict[str, Path], gap: float = 1.0, provenance: str = "fresh automated prediction") -> str:
        job_id = str(uuid.uuid4())[:8]
        self.jobs[job_id] = {
            "job_id": job_id,
            "status": "QUEUED",
            "progress_percent": 0,
            "message": "Job queued in serialized GPU execution queue",
            "provenance": provenance,
            "tp1_files": {k: str(v) for k, v in tp1_files.items()},
            "tp2_files": {k: str(v) for k, v in tp2_files.items()},
            "gap": gap,
            "result": None,
            "error": None,
            "created_at": time.time()
        }
        self.queue.put(job_id)
        return job_id

    def get_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.jobs.get(job_id)
        if not job:
            state_file = PROJECT_ROOT / "outputs" / "web_runs" / job_id / "job_state.json"
            if state_file.exists():
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        job = json.load(f)
                    self.jobs[job_id] = job
                except Exception:
                    pass
        if job and job.get("result"):
            res = job["result"]
            clf = res.get("classifiers", {})
            ext = clf.get("extended_task_b", {}).get("conformal_calibration", {})
            prov = str(job.get("provenance", "")).lower()
            if ("automated" in prov) or ("fresh" in prov) or ("gpu" in prov) or ("reference" not in prov):
                ext["status"] = "EXPERIMENTAL_AUTOMATED_INPUT_COVERAGE_NOT_VALIDATED"
                ext["label_bn"] = "পরীক্ষামূলক, coverage যাচাই হয়নি"
                ext["limitation"] = "Automated input conformal triage: Experimental research indicator only; coverage is not mathematically guaranteed (nominal 95% coverage calibrated on expert manual annotations only under exchangeability)."
                clf["limitation_statement"] = ext["limitation"]
        return job

    def _worker(self):
        runner = FrozenPipelineRunner.get_instance()
        while True:
            job_id = self.queue.get()
            job = self.jobs[job_id]
            job["status"] = "RUNNING"
            job["start_time"] = time.time()

            try:
                def set_prog(msg, pct):
                    job["message"] = msg
                    job["progress_percent"] = pct

                tp1_paths = {k: Path(v) for k, v in job["tp1_files"].items()}
                tp2_paths = {k: Path(v) for k, v in job["tp2_files"].items()}

                # Validation
                set_prog("Validating TP-1 baseline modalities...", 5)
                v1_ok, v1_msg, v1_meta = runner.validate_modalities(tp1_paths)
                if not v1_ok:
                    raise ValueError(f"Baseline validation error: {v1_msg}")

                set_prog("Validating TP-2 follow-up modalities...", 10)
                v2_ok, v2_msg, v2_meta = runner.validate_modalities(tp2_paths)
                if not v2_ok:
                    raise ValueError(f"Follow-up validation error: {v2_msg}")

                out_base = PROJECT_ROOT / "outputs" / "web_runs" / job_id
                out_base.mkdir(parents=True, exist_ok=True)

                # TP1 Scan
                set_prog("Running frozen ensemble on Baseline TP-1...", 15)
                tp1_order = [tp1_paths["t1n"], tp1_paths["t1c"], tp1_paths["t2w"], tp1_paths["flair"]]
                res_tp1, mask_tp1 = runner.predict_scan(tp1_order, out_base / "tp1", "tp1", lambda m, p: set_prog(f"TP-1: {m}", 15 + int(p*0.35)))

                # TP2 Scan
                set_prog("Running frozen ensemble on Follow-Up TP-2...", 55)
                tp2_order = [tp2_paths["t1n"], tp2_paths["t1c"], tp2_paths["t2w"], tp2_paths["flair"]]
                res_tp2, mask_tp2 = runner.predict_scan(tp2_order, out_base / "tp2", "tp2", lambda m, p: set_prog(f"TP-2: {m}", 55 + int(p*0.35)))

                # Volumetric change & physical endpoint
                set_prog("Calculating physical volumetrics & continuous growth margin...", 92)
                endpoint_eval = evaluate_imaging_growth_endpoint(res_tp1["volumes_ml"]["et"], res_tp2["volumes_ml"]["et"])

                # Feature extraction & classification
                set_prog("Evaluating frozen LightGBM classifiers (tau=0.42 / tau=0.35)...", 96)
                features = extract_task_b_features(res_tp1["volumes_ml"], res_tp2["volumes_ml"], gap=job["gap"])
                classifier_eval = runner.classifiers.predict(features, provenance=job.get("provenance", "fresh automated prediction"))

                # Assemble unified final payload
                total_duration = round(time.time() - job["start_time"], 2)
                job["status"] = "COMPLETED"
                job["progress_percent"] = 100
                job["message"] = f"Frozen pipeline execution complete ({total_duration}s)"
                job["result"] = {
                    "job_id": job_id,
                    "provenance": job["provenance"],
                    "total_execution_seconds": total_duration,
                    "endpoint_assessment": endpoint_eval,
                    "classifiers": classifier_eval,
                    "features_task_b": features,
                    "baseline_tp1": {
                        "volumes_ml": res_tp1["volumes_ml"],
                        "voxel_counts": res_tp1["voxel_counts"],
                        "inference_seconds": res_tp1["total_inference_sec"],
                        "fused_sha256": res_tp1["fused_sha256"]
                    },
                    "followup_tp2": {
                        "volumes_ml": res_tp2["volumes_ml"],
                        "voxel_counts": res_tp2["voxel_counts"],
                        "inference_seconds": res_tp2["total_inference_sec"],
                        "fused_sha256": res_tp2["fused_sha256"]
                    },
                    "system_info": {
                        "device": str(runner.device),
                        "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "None",
                        "pytorch_version": torch.__version__
                    }
                }

                job_state_file = out_base / "job_state.json"
                try:
                    with open(job_state_file, "w", encoding="utf-8") as jsf:
                        json.dump(job, jsf, indent=2, ensure_ascii=False)
                except Exception:
                    pass

            except Exception as e:
                import traceback
                job["status"] = "FAILED"
                job["error"] = str(e)
                job["traceback"] = traceback.format_exc()
                job["message"] = f"Execution failed: {str(e)}"

            finally:
                self.queue.task_done()

# ─── 7. DETERMINISTIC UNIT TEST SUITE ─────────────────────────────────────────
def run_unit_tests() -> Dict[str, Any]:
    """Runs strict boundary and deterministic calculations tests without rounded inputs."""
    results = []

    # Test 1: Exact absolute boundary (delta_ET = 0.5000 mL, rel = 50.0%)
    t1 = evaluate_imaging_growth_endpoint(1.0, 1.5)
    results.append(("Exact absolute boundary (delta=0.5, rel=50%)", t1["is_growth_positive"] is True and t1["continuous_growth_margin"] == 1.0))

    # Test 2: Exact relative boundary (delta_ET = 1.0000 mL, rel = 25.0%)
    t2 = evaluate_imaging_growth_endpoint(4.0, 5.0)
    results.append(("Exact relative boundary (delta=1.0, rel=25%)", t2["is_growth_positive"] is True and t2["continuous_growth_margin"] == 1.0))

    # Test 3: Only absolute met (delta_ET = 0.6 mL, rel = 6.0%) -> Must fail
    t3 = evaluate_imaging_growth_endpoint(10.0, 10.6)
    results.append(("Only abs met (delta=0.6, rel=6.0% < 25%)", t3["is_growth_positive"] is False and t3["continuous_growth_margin"] < 1.0))

    # Test 4: Only relative met (delta_ET = 0.2 mL, rel = 40.0% but < 0.5 mL) -> Must fail
    t4 = evaluate_imaging_growth_endpoint(0.1, 0.3)
    results.append(("Only rel met (delta=0.2 < 0.5 mL)", t4["is_growth_positive"] is False and t4["continuous_growth_margin"] < 1.0))

    # Test 5: Baseline ET zero (baseline = 0.0, follow = 0.5 mL -> floor 0.5 gives rel = 100%) -> Must pass
    t5 = evaluate_imaging_growth_endpoint(0.0, 0.5)
    results.append(("Zero baseline with 0.5 mL expansion", t5["is_growth_positive"] is True and t5["continuous_growth_margin"] == 1.0))

    # Test 6: Negative change (regression) -> Must fail
    t6 = evaluate_imaging_growth_endpoint(5.0, 3.0)
    results.append(("Negative volume regression", t6["is_growth_positive"] is False and t6["continuous_growth_margin"] < 0.0))

    # Test 7: Physical affine volumetry formula with mm
    dummy_mask = np.zeros((10, 10, 10), dtype=np.uint8)
    dummy_mask[:5, :5, :5] = 3 # 125 voxels of ET
    dummy_affine = np.diag([2.0, 2.0, 2.0, 1.0]) # 8 mm3 per voxel
    vol_calc = compute_physical_volumes(dummy_mask, dummy_affine, xyzt_units=("mm", "unknown"))
    expected_ml = 125 * 8.0 / 1000.0 # 1.000 mL
    results.append(("Physical affine volumetry (125 voxels @ 8mm3 = 1.0 mL, 'mm' unit)", abs(vol_calc["volumes_ml"]["et"] - expected_ml) < 1e-6))

    # Test 8: Physical volumetry rejecting unsupported spatial unit
    unit_rejected = False
    try:
        compute_physical_volumes(dummy_mask, dummy_affine, xyzt_units=("lightyears", "unknown"))
    except ValueError:
        unit_rejected = True
    results.append(("Physical volumetry rejects invalid spatial unit", unit_rejected))

    # Test 9: Exact Cell 2.4 feature extraction compaction formula verification
    test_v1 = {"et": 3.152, "snfh": 96.159, "rc": 14.404, "netc": 0.089}
    test_v2 = {"et": 18.019, "snfh": 38.667, "rc": 2.126, "netc": 1.504}
    feat_calc = extract_task_b_features(test_v1, test_v2, gap=1.0)
    # Audited Cell 2.4 manual calculation:
    v_exp = (3.152 + 14.404) * 1000.0 # 17556.0
    comp_exp = float(np.clip((np.pi**(1/3)) * ((6*v_exp)**(2/3)) / (4.836 * (v_exp**(2/3)) + 10.0 * 96.159), 0.05, 1.0))
    diff_comp = abs(feat_calc["compaction_proxy_base"] - comp_exp)
    results.append(("Exact Cell 2.4 compaction proxy formula check", diff_comp < 1e-12))

    # Test 10: Exact Cell 2.4 ratio floors (ET/SNFH floor 0.5, ET/RC floor 0.5)
    test_v_zero_denom = {"et": 2.0, "snfh": 0.05, "rc": 0.02, "netc": 0.0}
    feat_floors = extract_task_b_features(test_v_zero_denom, test_v2, gap=1.0)
    # Ratio denominator floor is 0.5 mL: 2.0 / 0.5 = 4.0
    results.append(("Ratio denominator floor is 0.5 mL (not 0.1)", abs(feat_floors["ratio_et_edema_base"] - 4.0) < 1e-6 and abs(feat_floors["ratio_et_cavity_base"] - 4.0) < 1e-6))

    # Test 11: Exact Cell 2.4 rel_snfh floor is 1.0 mL (not 0.5)
    # delta_snfh = 38.667 - 0.05 = 38.617. With floor 1.0, rel_snfh = 38.617 / 1.0 = 38.617
    results.append(("rel_snfh denominator floor is 1.0 mL (not 0.5)", abs(feat_floors["rel_snfh"] - 38.617) < 1e-6))

    # Test 12: Conformal calibration parameters check (alpha=0.05, exact q_hat=0.8341396203515523)
    clfs = FrozenClassifiers()
    pred_res = clfs.predict(feat_calc)
    conf_ok = False
    if pred_res.get("status") == "success":
        conf_meta = pred_res["extended_task_b"]["conformal_calibration"]
        conf_ok = (conf_meta["alpha"] == 0.05 and abs(conf_meta["conformal_cutoff_q_hat"] - 0.8341396203515523) < 1e-9 and conf_meta["target_nominal_coverage"] == 0.95 and conf_meta["conformal_cutoff_rounded"] == 0.8341)
    results.append(("Conformal calibration specification matches audited alpha=0.05, exact q_hat=0.8341396", conf_ok))

    all_pass = all(passed for _, passed in results)
    return {"all_passed": all_pass, "tests": results}

if __name__ == "__main__":
    test_res = run_unit_tests()
    print("Unit test summary:")
    for name, p in test_res["tests"]:
        print(f" - {name}: {'PASS' if p else 'FAIL'}")
    assert test_res["all_passed"], "Unit tests failed!"
    print(f"ALL {len(test_res['tests'])} UNIT TESTS PASSED DETERMINISTICALLY.")
