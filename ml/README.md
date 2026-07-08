ML folder for barcode-matching improvements.

Contents:
- export_training_dataset.py: exports labeled positives from match_audit.
- data/: generated dataset files for local experiments or Hugging Face upload.

Suggested workflow:
1. Run full sequence pipeline and accumulate match_audit rows.
2. Export data with:
   python ml/export_training_dataset.py --output ml/data/train_pairs_positive.csv
3. Add manually reviewed negatives and borderline examples.
4. Fine-tune or evaluate models using the curated dataset.
