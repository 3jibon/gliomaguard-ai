# GliomaGuard Research Surveillance Prototype

Deployable research prototype integrating the audited frozen 4-model segmentation ensemble, physical voxel-geometry volumetry, paper dual-criteria imaging growth endpoint, and dual LightGBM risk-triage classifiers.

---

## 1. System Requirements & Hardware Specification

- **Operating System:** Windows 10/11 (PowerShell/CMD) or Linux (Ubuntu 20.04/22.04 LTS).
- **Python Version:** Python 3.10 to 3.12 (Tested on Python 3.12.9 with standard virtualenv).
- **Compute Hardware:**
  - **GPU:** NVIDIA GPU with $\ge 4\text{ GB}$ VRAM (Tested and verified on NVIDIA GeForce RTX 2050 Laptop GPU, 4096 MB VRAM, CUDA 12.8, Driver 572.70).
  - **CPU Mode:** Fully supported fallback for deterministic math, feature extraction, API testing, and volumetry.
  - **RAM:** $\ge 8\text{ GB}$ system RAM.
- **Key Python Dependencies:**
  ```text
  torch>=2.0.0
  torchvision>=0.15.0
  numpy>=1.24.0
  scipy>=1.10.0
  nibabel>=5.0.0
  lightgbm>=4.0.0
  scikit-learn>=1.3.0
  pillow>=9.5.0
  ```

---

## 2. Checkpoints & Authoritative Artifact Manifest

All neural network checkpoints and tabular classifiers are frozen artifacts from the audited 293-patient external evaluation. **No models are retrained or tuned.**

| Model ID | Component Role | Architecture / Weights Path | SHA-256 Hash |
| :--- | :--- | :--- | :--- |
| **DL-5** | NETC Extraction | `nnunet_laptop/results/Dataset901_MUPostFrozen/nnUNetTrainerMedNeXtCompare300__nnUNetPlansLaptop2D__2d/best_within30_evaluation_e1ad58b0e0bb/checkpoint_frozen.pth` | `e1ad58b0e0bb2a7bc92bfd0ce1f0ffae8e59188486287d1fac81c847c4eb63b5` |
| **DL-2** | RC Extraction | `nnunet_laptop/results/Dataset901_MUPostFrozen/nnUNetTrainerResEncUNetCompare300__nnUNetPlansLaptop2D__2d/best_within30_evaluation_ecabd79ae499/checkpoint_frozen.pth` | `ecabd79ae4992bce74b02ad45e669da33186c7a68e3934bc593f982c78eb270e` |
| **DL-3** | SNFH Extraction | `nnunet_laptop/results/Dataset901_MUPostFrozen/nnUNetTrainerAttentionUNetCompare300__nnUNetPlansLaptop2D__2d/best_within30_evaluation_54843db06c80/checkpoint_frozen.pth` | `54843db06c80ae4aa91366b18dd45591e8be66562f6ee767d806d07ea909543b` |
| **DL-1** | ET Extraction | `nnunet_laptop/results/Dataset901_MUPostFrozen/nnUNetTrainer__nnUNetPlansLaptop2D__2d/best_within30_evaluation_7df04f00cc29/checkpoint_frozen.pth` | `7df04f00cc2989bc8a1a346a8c0434d9ad227a49756018ca748bd75964c5cd9e` |
| **Fusion** | Ordered Priority | `scripts/validate_trio_hybrid.py` (Rule: DL-5 NETC $\to$ DL-2 RC $\to$ DL-3 SNFH $\to$ DL-1 ET) | `3a47f917ff17eeb988cd9af0edab73a9338b9e6f905d448e36a11646fd568311` |
| **Baseline** | LightGBM Classifier | `webapp/models/baseline_model.pkl` ($\tau = 0.42$, 8 features) | `10b97ae7d697858c40ff383b2ff82bc1bba488d5e1ffafbe46479a957d549f39` |
| **Baseline** | StandardScaler | `webapp/models/baseline_scaler.pkl` | `d31a2abaf28833959fc93b6e7a2b97c2dc1b9b9a695126a111a87e5fe5f6ef26` |
| **Task B** | Extended LightGBM | `webapp/models/extended_lgbm_model.pkl` ($\tau = 0.35$, 15 features) | `93b5a8288b88d748f2178e22f283d5aeeae68fbff02a0a2dfae93708d7a18451` |
| **Task B** | StandardScaler | `webapp/models/extended_lgbm_scaler.pkl` | `27978617a7cbfce2b98816cbeae19453eb56b1836ec438848a6021a868f0aa12` |

---

## 3. Quick Startup Guide

### Step 1: Environment Activation
From the repository root:
```powershell
# Windows
.\.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate
```

### Step 2: Run Deterministic Unit Tests
Verify mathematical formulas, affine geometry, endpoint boundary cases, and classifier staging:
```bash
python webapp/frozen_pipeline.py
```
*Expected result:* `ALL 7/7 DETERMINISTIC TESTS PASSED.`

### Step 3: Launch Local Research Server
```bash
python webapp/server.py
```
Output:
```text
===========================================================================
GLIOMAGUARD PRODUCTION RESEARCH SERVER ACTIVE
===========================================================================
  Local URL       : http://localhost:8000
  Health Endpoint : http://localhost:8000/api/health
  Unit Tests API  : http://localhost:8000/api/pipeline/run_tests
  Cohort Catalog  : http://localhost:8000/api/pipeline/cohort_catalog
===========================================================================
```

### Step 4: Access in Web Browser
Open `http://localhost:8000` in Google Chrome, Microsoft Edge, or Firefox.

---

## 4. Operational Modes & Data Provenance

The prototype strictly separates and badges every output according to its provenance:

1. **Mode 1: Reference Benchmark Cockpit**
   - **Provenance Badge:** `PROVENANCE: reference/demo measurement (ground-truth manual annotation)`
   - Displays ground-truth manual segmentations and physical volumetrics from the audited reference cohorts.
   - Evaluates exact voxel-derived physical volumetry and paper dual-criteria endpoint on expert annotations.
   - Conformal calibration prediction sets ($\alpha = 0.05$, nominal 95% coverage, $\hat{q} = 0.8341$) are formally validated on this distribution.

2. **Mode 2: Cached Automated Pipeline**
   - **Provenance Badge:** `PROVENANCE: cached automated prediction (frozen four-model ensemble)`
   - Displays stored predictions from the frozen 4-model ensemble with ordered class-priority fusion from the audited 78-pair validation cohort.
   - Outputs separate and distinct automated measurements from reference measurements.

3. **Mode 3: Fresh Automated Pipeline (Live GPU Inference)**
   - **Provenance Badge:** `PROVENANCE: fresh automated prediction (live GPU inference)`
   - Executes live, serialized GPU inference across all 4 constituent 2D models on input multi-sequence NIfTI scans.
   - Serialized GPU execution (`_gpu_lock`) executes constituent passes sequentially (DL-5 $\to$ DL-2 $\to$ DL-3 $\to$ DL-1) to ensure safety on $\ge 4\text{ GB}$ VRAM GPUs without memory crashes.
   - Evaluates the dual-criteria imaging growth endpoint, extracts the 15 Task B delta-radiomics features via the audited formulas, and executes LightGBM inference.

---

## 5. MRI Geometry Validation & Security Rules

Before any inference or volumetry is accepted, scans undergo automated geometry validation:
1. **Modality Completeness:** All 4 required sequences (`t1n`, `t1c`, `t2w`, `flair`) must be present.
2. **Dimension Matching:** All 4 sequences must have identical 3D spatial dimensions $(H, W, D)$.
3. **Spatial Units:** NIfTI header units must explicitly specify millimeters (`'mm'`). Non-standard or undefined units are rejected.
4. **Pairwise Affine Co-Registration:** Every sequence must be co-registered to T1c with pairwise affine difference $< 0.01\text{ mm}$ (`np.allclose(aff, ref_aff, atol=1e-2)`).
5. **Determinant Non-Singularity:** The affine determinant $|\det(A)| \ge 1 \times 10^{-4}$ and must be non-NaN and finite.

---

## 6. Audited Mathematical Specifications

### A. Dual-Criteria Imaging-Defined Growth Endpoint
- **Condition 1 (Absolute):** $\Delta ET = ET_{\text{follow}} - ET_{\text{base}} \ge 0.5000\text{ mL}$
- **Condition 2 (Relative):** $\frac{\Delta ET}{\max(ET_{\text{base}}, 0.5000\text{ mL})} \ge 0.2500$ ($25.0\%$)
- **Continuous Margin:** $\min\left(\frac{\Delta ET}{0.5}, \frac{\Delta ET}{0.25 \times \max(ET_{\text{base}}, 0.5)}\right)$

### B. 15-Feature Task B Extraction (Notebook Cell 2.4 Formulation)
- $v = \max(ET_{\text{base}} + RC_{\text{base}}, 0.01) \times 1000.0$
- $\text{compaction\_proxy\_base} = \text{clip}\left(\frac{\pi^{1/3} (6v)^{2/3}}{4.836 v^{2/3} + 10.0 \times SNFH_{\text{base}}}, 0.05, 1.0\right)$
- $\text{ratio\_et\_edema\_base} = \frac{ET_{\text{base}}}{\max(SNFH_{\text{base}}, 0.5)}$ (denominator floor $0.5\text{ mL}$)
- $\text{ratio\_et\_cavity\_base} = \frac{ET_{\text{base}}}{\max(RC_{\text{base}}, 0.5)}$ (denominator floor $0.5\text{ mL}$)
- $\text{rel\_snfh} = \frac{\Delta SNFH}{\max(SNFH_{\text{base}}, 1.0)}$ (denominator floor $1.0\text{ mL}$)
- $\text{vel\_*} = \frac{\Delta *}{\max(\text{gap}, 1.0)}$

### C. Conformal Calibration Specification
- Significance level: $\alpha = 0.05$ (Target nominal coverage $0.95$).
- Calibration cohort: 31 patients, 82 longitudinal pairs; finite-sample quantile cutoff $\hat{q} = 0.8341$.
- Validation performance: Empirical coverage $97.44\%$ (76/78 pairs), single-label efficiency $44.87\%$ (35/78), ambiguous/flagged rate $55.13\%$ (43/78).
- **Exchangeability Limitation:** Conformal prediction sets are validated exclusively for reference-input evaluation under exchangeability. They do not provide unconditional coverage guarantees and cannot be assumed to hold on automated predicted inputs.

---

## 7. REST API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/health` | Returns server health, active device, loaded models, checkpoint verification, and thresholds. |
| `GET` | `/api/pipeline/run_tests` | Executes the 12/12 deterministic math, geometry, and API verification suite. |
| `GET` | `/api/pipeline/cohort_catalog` | Lists available benchmark cases (`UCSF_100026`, `PatientID_0018`, `PatientID_0022`). |
| `GET` | `/api/pipeline/load_case?case_id=<ID>&mode=<MODE>` | Returns physical volumes, endpoint, classifiers, and provenance for a case. |
| `POST` | `/api/pipeline/job_submit` | Enqueues a fresh 4-model serialized GPU inference job. |
| `GET` | `/api/pipeline/job_status?job_id=<ID>` | Polls status, stage progress percentage, and results of a GPU job. |
| `POST` | `/api/longitudinal_assess` | Reconciles raw compartment volumes into paper dual-criteria endpoint and LightGBM predictions. |

---

## 8. Research Limitations & Governance

- **Research Prototype Only:** Not cleared as a medical device. Not intended for clinical diagnosis, progressive disease determination, or pseudoprogression staging.
- **Conformal Guarantees:** Conformal coverage bounds ($\alpha = 0.05$, 95% nominal coverage) reported in research are mathematically established for manual reference inputs under exchangeability only. They do not provide unconditional coverage guarantees and cannot be assumed to hold on automated predictions.
- **Voxel Volumetry:** Physical volumetry strictly requires genuine NIfTI affine geometry: $V = N_{\text{voxels}} \times |\det(\text{affine}[:3,:3])| / 1000$. 2D hardcopy camera photo heuristics ($\text{pixel} \times 0.045$) are unvalidated demonstration aids and are excluded from scientific analysis.
- **No Latency-Based Clinical Claims:** Inference latency is unoptimized and reported purely as a research benchmark. No claims of real-time or edge deployment are made in the scientific manuscript.
