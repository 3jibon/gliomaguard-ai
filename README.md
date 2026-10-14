---
title: GliomaGuard AI - Longitudinal Surveillance Engine
emoji: 🧠
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# GliomaGuard AI™ — Longitudinal Glioma Surveillance System

Clinical translational decision support web application embodying the research paper's core `Trio-GliomaNet` deep learning ensemble and RANO 2.0 longitudinal surveillance framework.

## Key Capabilities
- **100% Genuine Neural Inference**: Real-time multi-compartment segmentation (ET, Edema, Cavity, NETC) powered by PyTorch without pre-baked lookups.
- **Dual Multi-Timepoint Comparison**: Side-by-side comparison between Baseline (TP-1) and Follow-Up (TP-2) surveillance MRIs.
- **Clinical RANO 2.0 Response Assessment**: Quantifies exact volumetric $\Delta \text{ET}\%$, $\Delta \text{Edema}\%$, and $\Delta \text{Cavity}\%$ with 12-week post-radiotherapy pseudo-progression guardrails.
- **Transparent Explainable AI (XAI)**: Live backpropagation gradient saliency heatmaps with peak anatomical coordinates.
- **Stream 2 Optical Hardcopy Decomposition (ODRP)**: Ingests 4x5 film sheet grids captured via smartphone cameras, decomposes them into axial slices, and rectifies glare/illumination.

## Free 1-Click Hosting Guide
This directory is fully self-contained with model weights (`best.pt`, 5.88 MB) and can be hosted 100% for free on:
1. **Hugging Face Spaces**:
   - Create a new Space on Hugging Face (SDK: Docker).
   - Push the contents of the `webapp/` folder to the Space repository.
   - It will automatically build and provide an instant public HTTPS URL.
2. **Render / Railway**:
   - Link your GitHub repo to a new Web Service on Render or Railway.
   - Set the root directory to `webapp`.
   - Build using the included `Dockerfile`.




access link: https://gliomaguard-ai.onrender.com
