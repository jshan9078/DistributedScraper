"""
Count Cards by PSA Grade

This script scans a Google Cloud Storage bucket and counts the total number of
complete certificate cards for each PSA grade. A complete card must have both
front and back images.

Prerequisites:
- Google Cloud Storage credentials JSON file
- google-cloud-storage Python package

Setup:
1. Install required package:
   pip install google-cloud-storage

2. Set your Google Cloud credentials:
   export GOOGLE_APPLICATION_CREDENTIALS="/path/to/your/credentials.json"

Usage:
   python count_cards_by_grade.py

The script will:
- Scan all PNG files in the bucket under the png/ prefix
- Identify complete certificates (both front and back exist)
- Count unique certificates per PSA grade
- Display a summary table of results
"""

from google.cloud import storage
from collections import defaultdict

BUCKET_NAME = "psa-scan-scraping-dataset"

def count_cards_by_grade(bucket_name):
    """
    Count complete certificate cards grouped by PSA grade.

    A card is only counted if it has both front and back images.
    Cards are not double-counted since we count by unique cert_id.

    Args:
        bucket_name (str): Name of the GCS bucket to scan

    Returns:
        dict: Dictionary mapping grade -> count of complete certificates
    """
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    # List all blobs under png/
    blobs = bucket.list_blobs(prefix="png/")
    cert_map = defaultdict(lambda: {"front": False, "back": False, "grades": set()})

    print("🔍 Scanning bucket for certificate images...")
    total_files = 0

    for blob in blobs:
        path = blob.name  # e.g. png/10/123456789_front.png
        if not path.endswith(".png"):
            continue

        total_files += 1

        try:
            parts = path.split("/")
            grade = parts[1]
            filename = parts[-1]
            cert_id, side = filename.replace(".png", "").split("_")

            cert_map[cert_id]["grades"].add(grade)
            cert_map[cert_id][side] = True

        except Exception as e:
            print(f"⚠️ Skipping malformed path: {path} ({e})")

    print(f"📁 Scanned {total_files} total image files")
    print()

    # Count complete certificates by grade
    grade_counts = defaultdict(int)
    incomplete_certs = []

    for cert_id, info in cert_map.items():
        if info["front"] and info["back"]:  # Both sides exist
            # A cert should only be in one grade folder
            if len(info["grades"]) == 1:
                grade = list(info["grades"])[0]
                grade_counts[grade] += 1
            else:
                print(f"⚠️ Warning: Certificate {cert_id} found in multiple grades: {info['grades']}")
        else:
            incomplete_certs.append(cert_id)

    # Display results
    print("=" * 50)
    print("📊 COMPLETE CARDS BY PSA GRADE")
    print("=" * 50)

    total_complete = 0
    for grade in sorted(grade_counts.keys(), key=lambda x: int(x) if x.isdigit() else 0):
        count = grade_counts[grade]
        total_complete += count
        print(f"Grade {grade:>2}: {count:>5} cards")

    print("=" * 50)
    print(f"TOTAL:     {total_complete:>5} complete cards")
    print("=" * 50)

    if incomplete_certs:
        print()
        print(f"⚠️ Found {len(incomplete_certs)} incomplete certificates (missing front or back)")

    return grade_counts

if __name__ == "__main__":
    count_cards_by_grade(BUCKET_NAME)
