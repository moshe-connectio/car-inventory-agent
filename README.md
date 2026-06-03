# Car Agent

סוכן אוטומציה ב-Python שמסנכרן את **קטלוג הרכבים בעברית ב-Zoho CRM** עם נתוני
השוק הישראלי בפועל. מקבל רשומת יצרן מ-Zoho (דרך Deluge webhook), מגלה אוטומטית
אילו **דגמים** ו**רמות גימור (trims)** נמכרים כיום בישראל — כולל מחיר, מפרט טכני
ותמונה — ויוצר / מעדכן / מדליק / מכבה רשומות ב-Zoho בהתאם.

> מקור הסמכא העיקרי הוא **icar.co.il** (scrape ישיר). כש-icar ריק, נופלים ל-AI עם
> חיפוש אינטרנט (OpenAI) המאומת מול היבואן הרשמי ו-auto.co.il.

---

## ארכיטקטורה

```
Zoho CRM (Deluge)  ──POST──►  FastAPI webhook (main.py)
                                     │  מזהה רשומת יצרן → שולף דגמים מ-Zoho
                                     ▼
                              Celery + Redis  (tasks.py)
                              ┌──────────┴──────────┐
                              ▼                     ▼
                      סוכן דגמים            סוכן גרסאות (trims)
                  (agents/model_openai)   (agents/trim_openai)
                              │                     │
                              └────► Zoho CRM ◄──────┘
                          create / update / activate / deactivate
```

**שרשור אוטומטי:** לאחר סריקת דגמים, כל דגם שנוצר או הודלק מפעיל מיד סריקת trims
משלו (`_chain_trim_scan` ב-`tasks.py`).

---

## מבנה הקוד

| נתיב | תפקיד |
|------|-------|
| `main.py` | שרת FastAPI. נקודות קצה: `POST /webhook/models`, `POST /webhook/trims`, `GET /health`, `GET /status/{task_id}`. מזהה אם התקבל payload מובנה או רשומת יצרן גולמית מ-Deluge, ובמקרה השני שולף את הדגמים מ-Zoho ובונה payload. דוחף משימה ל-Celery. |
| `tasks.py` | אפליקציית Celery (broker = Redis). משימות: `scan_models_for_manufacturer`, `scan_trims_for_manufacturer` (retry x3, 5 דק'), `health_check`. בוחר provider לפי `AI_PROVIDER`. |
| `zoho_client.py` | עטיפה מעל Zoho CRM v7 Functions (יצרנים / דגמים / גרסאות). ה-`zapikey` נטען מ-`ZOHO_APIKEY`. |
| **`agents/model_openai/`** | **סוכן הדגמים (OpenAI — נתיב פעיל):** |
| &nbsp;&nbsp;`icar.py` | scrape ישיר של icar.co.il (primary) + fallback לגילוי דגמים ב-AI. |
| &nbsp;&nbsp;`runner.py` | מנוע ההשוואה. 7 שכבות התאמה בין שמות אנגלית (Zoho) לעברית (icar), כולל טיפול בעברית משובשת (`????`) ב-Zoho, ו-guard נגד כיבוי שגוי שיוצר trims יתומים. |
| &nbsp;&nbsp;`details.py` | שליפת פרטי דגם (תיאור עברי, סוג הנעה, קטגוריות, שנת השקה בישראל) דרך AI + web search. |
| &nbsp;&nbsp;`images.py` | שרשרת תמונות: scrape של auto.co.il → AI search → carimagesapi.com (מוריד מקומית). מסנן placeholders ולוגואים. |
| &nbsp;&nbsp;`utils.py` | קבועים (סוגי הנעה, קטגוריות תקינות), `ai_call`, `parse_json` (עם `json_repair`), נרמול שמות. |
| **`agents/trim_openai/`** | **סוכן הגרסאות:** |
| &nbsp;&nbsp;`fetcher.py` | קריאת AI אחת לכל דגם: מחירים מ-icar.co.il + מפרט מ-auto.co.il. אכיפה: רק מקורות מאושרים (icar / auto / gov.il), מחירים ₪55K–₪1.5M, אסור להמציא ערכים. |
| &nbsp;&nbsp;`runner.py` | סנכרון trims מול Zoho: יצירה / עדכון שדות / הדלקה / כיבוי. |
| `agents/model_agent_openai.py`, `trim_agent_openai.py` | נקודות כניסה דקות שמייצאות `run` עבור `tasks.py`. |
| `agents/model_agent.py`, `image_agent.py`, `manufacturer_agent.py`, `agents/scrapers/` | **Legacy** מבוסס Anthropic / BeautifulSoup — נבחר רק כש-`AI_PROVIDER=anthropic`. |
| `scripts/refresh_images.py` | מיגרציה חד-פעמית: ממלא תמונות חסרות לכל הדגמים הפעילים. `--dry-run` נתמך. |
| `setup.sh` | הקמת הסביבה על Droplet (חבילות, venv, systemd, nginx). |

---

## מקורות נתונים

- **icar.co.il** — דגמים פעילים + מחירים (מקור סמכא, scrape ישיר ללא AI).
- **auto.co.il** — מפרט טכני ותמונות.
- **carimagesapi.com** — fallback לתמונות (signed URL, מורד ונשמר מקומית).
- **OpenAI** (`gpt-4o-mini`, `responses` API + `web_search_preview`) — גילוי ואימות כש-icar חסר.
- **Zoho CRM v7 Functions** — מקור האמת לרשומות וכתיבה חזרה.

---

## משתני סביבה

מוגדרים ב-`/opt/car-agent/.env` (לא ב-git; נטען ע"י systemd דרך `EnvironmentFile`).
ראה `.env.example` לתבנית.

| משתנה | תיאור |
|-------|-------|
| `AI_PROVIDER` | `openai` (פעיל) או `anthropic` (legacy). |
| `OPENAI_API_KEY` | מפתח OpenAI. |
| `OPENAI_MODEL` | ברירת מחדל `gpt-4o-mini`. |
| `ZOHO_APIKEY` | ה-`zapikey` של Zoho custom functions. **חובה** — הקוד נכשל בלעדיו. |
| `CARIMAGES_API_KEY` / `CARIMAGES_API_SECRET` | carimagesapi.com. בלעדיהם, fallback התמונות מדלג (לא קורס). |
| `IMAGES_DIR` | תיקיית שמירת תמונות (ברירת מחדל `/var/www/car-images`). |
| `IMAGES_BASE_URL` | בסיס URL ציבורי לתמונות (`https://images.gsmdev.co.il/car-images`). |

---

## פריסה

נפרס על DigitalOcean Droplet. שלושה שירותי systemd + nginx + redis:

| שירות | תפקיד |
|-------|-------|
| `car-webhook` | uvicorn על `127.0.0.1:8000` (מאחורי nginx על פורט 80). |
| `car-worker` | Celery worker שמריץ את הסוכנים. |
| `car-beat` | scheduler (מושבת בייצור כרגע). |

הקמה ראשונית:

```bash
cd /opt/car-agent
cp .env.example .env          # מלא ערכים אמיתיים
chmod 600 .env
bash setup.sh
```

בדיקות:

```bash
curl http://localhost/health                       # {"status":"ok"}
systemctl status car-webhook car-worker --no-pager
journalctl -u car-worker -f                        # מעקב לוגים
```

הפעלת מיגרציית תמונות:

```bash
cd /opt/car-agent && venv/bin/python scripts/refresh_images.py --dry-run
```

---

## אבטחה

- כל הסודות ב-`.env` בלבד (gitignored, הרשאות `600`), נטענים ל-systemd דרך
  `EnvironmentFile`. אין סודות בקוד או ב-unit files.
- **שים לב:** סודות שהיו בעבר בקוד נמצאים עדיין ב-**היסטוריית git**. נדרש **סבב מפתחות**
  (rotation) אצל הספקים: Zoho `zapikey`, carimagesapi key/secret, ו-OpenAI key.
