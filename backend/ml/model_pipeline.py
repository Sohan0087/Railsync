"""
RailSync predictive model pipeline.

Real-data path:
  Uses the Kaggle dataset "salmaneunus/railway-track-fault-detection"
  (defective vs non-defective rail images, used in published railway
  fault-detection research) via the official `kaggle` API + the
  credentials the user supplied in kaggle.json. Each image is reduced to
  a small set of interpretable numeric features (brightness, contrast,
  edge density, dark-pixel ratio — proxies for surface cracks/flaking)
  and a RandomForestClassifier is trained to predict defect probability.

Fallback path:
  If `kaggle` isn't installed, credentials aren't reachable, or there's
  no network (as in this build sandbox), the same training code runs on
  a synthetically generated but domain-realistic dataset (crack length,
  defect area %, surface roughness, corrosion index -> defective/ok) so
  every part of the app — training, metrics, inference — stays 100%
  functional offline. The dashboard always shows which source produced
  the active model.

Either way the output is the same: model.pkl + metrics.json, and a
`predict(features)` function used by the /api/inspect endpoint.
"""
import os
import io
import json
import random
import time
from datetime import datetime

import numpy as np
import pandas as pd
from joblib import dump, load
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

ML_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ML_DIR, "data")
MODEL_PATH = os.path.join(ML_DIR, "model.pkl")
METRICS_PATH = os.path.join(ML_DIR, "metrics.json")
KAGGLE_DATASET = "salmaneunus/railway-track-fault-detection"

FEATURE_NAMES = ["mean_brightness", "contrast_std", "edge_density", "dark_pixel_ratio"]


# ---------------------------------------------------------------- utils --
def _image_features(img):
    """Turn a PIL image into 4 interpretable numeric features."""
    from PIL import ImageFilter
    gray = img.convert("L").resize((128, 128))
    arr = np.asarray(gray, dtype=np.float32) / 255.0
    edges = np.asarray(gray.filter(ImageFilter.FIND_EDGES), dtype=np.float32) / 255.0
    return [
        float(arr.mean()),
        float(arr.std()),
        float(edges.mean()),
        float((arr < 0.35).mean()),
    ]


def _try_fetch_kaggle_images(max_per_class=250):
    """Attempt the real download+extraction path. Returns a DataFrame or raises."""
    os.environ.setdefault(
        "KAGGLE_CONFIG_DIR", os.path.join(os.path.dirname(ML_DIR), "kaggle_config")
    )
    import kaggle  # raises ImportError if not installed
    from PIL import Image

    kaggle.api.authenticate()
    os.makedirs(DATA_DIR, exist_ok=True)
    kaggle.api.dataset_download_files(KAGGLE_DATASET, path=DATA_DIR, unzip=True, quiet=True)

    rows = []
    for root, _dirs, files in os.walk(DATA_DIR):
        label = None
        lower_root = root.lower()
        if "non" in lower_root and "defect" in lower_root:
            label = 0
        elif "defect" in lower_root:
            label = 1
        if label is None:
            continue
        count = 0
        for fname in files:
            if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            if count >= max_per_class:
                break
            try:
                with Image.open(os.path.join(root, fname)) as img:
                    rows.append(_image_features(img) + [label])
                count += 1
            except Exception:
                continue

    if not rows:
        raise RuntimeError("Kaggle dataset downloaded but no labelled images were found.")
    return pd.DataFrame(rows, columns=FEATURE_NAMES + ["label"])


def _synthetic_dataset(n=1200, seed=42):
    """Domain-realistic fallback: correlated features so the model has real
    signal to learn, without needing network access or a GPU."""
    rng = np.random.default_rng(seed)
    n_defect = n // 2
    n_ok = n - n_defect

    # Defective rail surfaces: darker (rust/wear), higher contrast (cracks),
    # more edges (fissures), more dark pixels (pitting/flaking).
    defect = pd.DataFrame({
        "mean_brightness": rng.normal(0.42, 0.08, n_defect).clip(0, 1),
        "contrast_std": rng.normal(0.28, 0.05, n_defect).clip(0, 1),
        "edge_density": rng.normal(0.24, 0.06, n_defect).clip(0, 1),
        "dark_pixel_ratio": rng.normal(0.38, 0.09, n_defect).clip(0, 1),
        "label": 1,
    })
    ok = pd.DataFrame({
        "mean_brightness": rng.normal(0.62, 0.07, n_ok).clip(0, 1),
        "contrast_std": rng.normal(0.14, 0.04, n_ok).clip(0, 1),
        "edge_density": rng.normal(0.11, 0.04, n_ok).clip(0, 1),
        "dark_pixel_ratio": rng.normal(0.15, 0.06, n_ok).clip(0, 1),
        "label": 0,
    })
    df = pd.concat([defect, ok], ignore_index=True).sample(frac=1, random_state=seed).reset_index(drop=True)
    return df


def _kaggle_credentials_present():
    cfg_dir = os.environ.get(
        "KAGGLE_CONFIG_DIR",
        os.path.join(os.path.dirname(ML_DIR), "kaggle_config"),
    )
    return os.path.isfile(os.path.join(cfg_dir, "kaggle.json"))


# ------------------------------------------------------------ pipeline --
def train():
    """Train the defect classifier. Uses Kaggle only if credentials exist;
    otherwise trains instantly on the synthetic fallback (avoids hang)."""
    started = time.time()
    source = "synthetic_fallback"
    note = (
        "Trained on synthetic, domain-realistic fallback data (offline mode). "
        "Add backend/kaggle_config/kaggle.json to use real track images."
    )
    df = None

    if _kaggle_credentials_present():
        source = "kaggle"
        note = f"Trained on real images from Kaggle dataset '{KAGGLE_DATASET}'."
        try:
            df = _try_fetch_kaggle_images()
            if df is None or len(df) < 20:
                raise RuntimeError("Kaggle images parsed but too few usable samples.")
        except Exception as e:
            source = "synthetic_fallback"
            note = (
                f"Kaggle unavailable ({type(e).__name__}: {e}). "
                "Trained on synthetic fallback instead."
            )
            df = None

    if df is None:
        df = _synthetic_dataset()

    X = df[FEATURE_NAMES].values
    y = df["label"].values
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=42, stratify=y
    )

    clf = RandomForestClassifier(n_estimators=120, max_depth=6, random_state=42, n_jobs=1)
    clf.fit(X_train, y_train)
    preds = clf.predict(X_test)

    metrics = {
        "source": source,
        "note": note,
        "dataset": KAGGLE_DATASET if source == "kaggle" else "synthetic_fallback_v1",
        "n_samples": int(len(df)),
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "accuracy": round(float(accuracy_score(y_test, preds)), 4),
        "precision": round(float(precision_score(y_test, preds, zero_division=0)), 4),
        "recall": round(float(recall_score(y_test, preds, zero_division=0)), 4),
        "f1": round(float(f1_score(y_test, preds, zero_division=0)), 4),
        "feature_importances": {
            name: round(float(imp), 4)
            for name, imp in zip(FEATURE_NAMES, clf.feature_importances_)
        },
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "train_seconds": round(time.time() - started, 2),
    }

    dump(clf, MODEL_PATH)
    with open(METRICS_PATH, "w") as f:
        json.dump(metrics, f, indent=2)
    return metrics


def get_status():
    if not os.path.exists(METRICS_PATH):
        return {"trained": False}
    with open(METRICS_PATH) as f:
        metrics = json.load(f)
    metrics["trained"] = True
    return metrics


def predict_from_image_bytes(image_bytes):
    from PIL import Image
    if not os.path.exists(MODEL_PATH):
        raise RuntimeError("Model has not been trained yet. Train it from the dashboard first.")
    clf = load(MODEL_PATH)
    img = Image.open(io.BytesIO(image_bytes))
    feats = _image_features(img)
    proba = clf.predict_proba([feats])[0]
    defect_prob = float(proba[1]) if len(proba) > 1 else float(proba[0])
    return {
        "features": dict(zip(FEATURE_NAMES, feats)),
        "defect_probability": round(defect_prob, 4),
        "predicted_label": "Defective" if defect_prob >= 0.5 else "Non-defective",
    }


def predict_from_random_demo_sample():
    """Used only when a user wants to try inference without uploading a photo —
    draws one held-out-style synthetic sample and scores it, for demo purposes."""
    if not os.path.exists(MODEL_PATH):
        raise RuntimeError("Model has not been trained yet. Train it from the dashboard first.")
    clf = load(MODEL_PATH)
    df = _synthetic_dataset(n=20, seed=random.randint(1, 100000))
    row = df.sample(1).iloc[0]
    feats = [row[c] for c in FEATURE_NAMES]
    proba = clf.predict_proba([feats])[0]
    defect_prob = float(proba[1]) if len(proba) > 1 else float(proba[0])
    return {
        "features": dict(zip(FEATURE_NAMES, feats)),
        "true_label": "Defective" if row["label"] == 1 else "Non-defective",
        "defect_probability": round(defect_prob, 4),
        "predicted_label": "Defective" if defect_prob >= 0.5 else "Non-defective",
    }
