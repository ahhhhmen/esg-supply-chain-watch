# Heavy imports are moved to direct sub-module imports by consumers:
#   from backend.utils.references import clean_title, normalize_url, ...
#   from backend.utils.utils import generate_pdf_from_md, clean_text
# This keeps the package lightweight for tools that only need references.py. 