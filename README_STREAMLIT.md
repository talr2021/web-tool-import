# Streamlit UI לכלי ההעלאה (URL → Woo CSV + תמונות 1080×1080)

## התקנה והרצה
```bash
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## שימוש
1. הדביקו בשדה הראשי לינקים למוצר (שורה אחת לכל מוצר).
2. בצד שמאל הגדירו קטגוריה/תגים/קידומת SKU (אופציונלי).
3. לחצו “הפעל”. לכל מוצר יווצרו:
   - CSV מוכן ל-WooCommerce (עם וריאציות אם אותרו)
   - ZIP עם כל התמונות המעובדות 1080×1080 (רקע לבן)
4. ניתן להוריד לכל מוצר בנפרד או להוריד ZIP כולל לכלל התוצרים.

## הערות
- מומלץ ייבוא עם **WP All Import**.
- זיהוי וריאציות תומך במבנה WooCommerce סטנדרטי (`data-product_variations`) ו־JSON-LD בסיסי.
- ניתן להרחיב בהמשך: חיבור API ל-WooCommerce להעלאה ישירה, שכתוב תיאורים בעזרת מודל שפה, מיפוי אוטומטי של תכונות (צבע/מידה) ועוד.
