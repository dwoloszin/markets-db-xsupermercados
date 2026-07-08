---
title: markets-db-barcode-matcher
emoji: 📦
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: 4.44.1
python_version: 3.11
app_file: app.py
pinned: false
---

Semantic product-to-barcode matcher prototype for markets_db.

Input format (Python dict string):

{
  "target": {
    "product_name": "Aveia Em Flocos Yoki Finos 170g",
    "brand": "Yoki",
    "description": "",
    "unit": "UN"
  },
  "candidates": [
    {
      "barcode": "7891095028337",
      "source_market": "Rossi",
      "source_market_id": "rossi_7891095028337",
      "product_name": "Aveia Em Flocos Yoki Finos 170g",
      "brand": "Yoki",
      "description": ""
    }
  ]
}
