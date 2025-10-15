"""
Cleanup Script for Incomplete Certificate Images

This script scans a Google Cloud Storage bucket for certificate images and deletes
any certificates that only have one side (front OR back, but not both).

Prerequisites:
- Google Cloud Storage credentials JSON file
- google-cloud-storage Python package

Setup:
1. Install required package:
   pip install google-cloud-storage

2. Set your Google Cloud credentials:
   export GOOGLE_APPLICATION_CREDENTIALS="/path/to/your/credentials.json"

Usage:
   python cleanup_incomplete_certs.py

The script will:
- Scan all PNG files in the bucket under the png/ prefix
- Identify certificates with only front or back images
- Delete incomplete certificate images
- Report the number of files deleted
"""

from google.cloud import storage
from collections import defaultdict

BUCKET_NAME = "psa-scan-scraping-dataset"

def clean_incomplete_cert_images(bucket_name):
    """
    Clean up incomplete certificate images from GCS bucket.

    A certificate is considered incomplete if it only has a front OR back image,
    but not both. This function deletes all images for incomplete certificates.

    Args:
        bucket_name (str): Name of the GCS bucket to clean
    """
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    # List all blobs under png/
    blobs = bucket.list_blobs(prefix="png/")
    cert_map = defaultdict(lambda: {"front": False, "back": False, "grades": set(), "paths": []})

    print("🔍 Scanning bucket...")
    for blob in blobs:
        path = blob.name  # e.g. png/10/123456789_front.png
        if not path.endswith(".png"):
            continue
        try:
            parts = path.split("/")
            grade = parts[1]
            filename = parts[-1]
            cert_id, side = filename.replace(".png", "").split("_")
            cert_map[cert_id]["grades"].add(grade)
            cert_map[cert_id][side] = True
            cert_map[cert_id]["paths"].append(path)
        except Exception as e:
            print(f"⚠️ Skipping malformed path: {path} ({e})")

    # Find certs with only one side
    to_delete = []
    for cert_id, info in cert_map.items():
        if info["front"] ^ info["back"]:  # XOR — only one side exists
            to_delete.extend(info["paths"])

    print(f"🧮 Found {len(to_delete)} incomplete entries to delete.")

    if to_delete:
        for path in to_delete:
            blob = bucket.blob(path)
            blob.delete()
            print(f"🗑️ Deleted {path}")
    else:
        print("ℹ️ No incomplete certificates found.")

    print("✅ Cleanup complete.")

if __name__ == "__main__":
    clean_incomplete_cert_images(BUCKET_NAME)
