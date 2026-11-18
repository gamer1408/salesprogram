import os
import json
import asyncio
from datetime import datetime, date
import pytz
import gspread
from google.oauth2.service_account import Credentials

TZ = pytz.timezone("Asia/Tashkent")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CLIENTS_HEADERS = [
    "ID",
    "Qoshilgan_Sana",
    "Oxirgi_Aloqa_Sana",
    "Ism",
    "Telefon",
    "TG_Username",
    "Viloyat_Tuman",
    "Nav",
    "Soni",
    "Narx_1_Dona",
    "Jami_Summa",
    "Zaklad_Summa",
    "Qoldiq_Summa",
    "Paket_Ogit_Info",
    "Vadalar_JSON",
    "Qongiroq_1",
    "Qongiroq_2",
    "Qongiroq_3",
    "Qongiroq_4",
    "Status",
    "Keyingi_Qongiroq_Vaqti",
]

SETTINGS_HEADERS = ["Kalit", "Qiymat"]

DEFAULT_SETTINGS = [
    ["OYLIK_KOCHAT_MAQSAD", "50000"],
    [
        "QONGIROQ_1_REJA",
        "Maqsad: Yer maydoni (sotix), tuproq turi va suv manbaini aniqlang (SPIN). Narxni eng oxirida ayting!",
    ],
    [
        "QONGIROQ_2_REJA",
        "Maqsad: Atlant/Maravilla afzalligi (1–1.5 kg hosil) va go‘ng/gumus paketini tushuntirib, zakladga olib keling.",
    ],
    [
        "QONGIROQ_3_REJA",
        "Maqsad: E’tirozlarni yoping (1-yildayoq o‘zini oqlashini hisoblab bering) va aniq zaklad sanasini oling.",
    ],
    [
        "QONGIROQ_4_REJA",
        "Maqsad: Yuklash vaqti, transport va qoldiq to‘lovni tasdiqlang. Bajarilmagan va’dalarni tekshiring!",
    ],
]


def now_tz() -> datetime:
    return datetime.now(TZ)


def today_str() -> str:
    return now_tz().strftime("%Y-%m-%d")


def parse_date_safe(date_str: str) -> date | None:
    if not date_str:
        return None
    clean = str(date_str).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            continue
    return None


def parse_datetime_safe(dt_str: str) -> datetime | None:
    if not dt_str:
        return None
    clean = str(dt_str).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(clean, fmt)
            return TZ.localize(dt)
        except ValueError:
            continue
    return None


def to_int(val) -> int:
    if val is None or val == "":
        return 0
    try:
        cleaned = "".join(ch for ch in str(val) if ch.isdigit() or ch == "-")
        return int(cleaned) if cleaned else 0
    except ValueError:
        return 0


class SheetsDB:
    def __init__(self):
        self.sheet_name = os.getenv("SPREADSHEET_NAME", "MalinachiBaza")
        self.creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")
        self.creds_json_env = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
        self.client = None
        self.spreadsheet = None
        self.ws_clients = None
        self.ws_settings = None
        self._lock = asyncio.Lock()

    def _connect_sync(self):
        if self.creds_json_env:
            info = json.loads(self.creds_json_env)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(self.creds_file, scopes=SCOPES)

        self.client = gspread.authorize(creds)
        try:
            self.spreadsheet = self.client.open(self.sheet_name)
        except gspread.SpreadsheetNotFound:
            self.spreadsheet = self.client.create(self.sheet_name)

        # 1-varaq: Sale_P_Mijozlar
        try:
            self.ws_clients = self.spreadsheet.worksheet("Sale_P_Mijozlar")
        except gspread.WorksheetNotFound:
            self.ws_clients = self.spreadsheet.add_worksheet(
                title="Sale_P_Mijozlar", rows=1000, cols=len(CLIENTS_HEADERS)
            )
            self.ws_clients.append_row(CLIENTS_HEADERS, value_input_option="USER_ENTERED")

        first_row = self.ws_clients.row_values(1)
        if not first_row:
            self.ws_clients.append_row(CLIENTS_HEADERS, value_input_option="USER_ENTERED")

        # 2-varaq: CRM_Sozlamalar
        try:
            self.ws_settings = self.spreadsheet.worksheet("CRM_Sozlamalar")
        except gspread.WorksheetNotFound:
            self.ws_settings = self.spreadsheet.add_worksheet(
                title="CRM_Sozlamalar", rows=50, cols=2
            )
            self.ws_settings.append_row(SETTINGS_HEADERS, value_input_option="USER_ENTERED")
            for row in DEFAULT_SETTINGS:
                self.ws_settings.append_row(row, value_input_option="USER_ENTERED")

        s_first = self.ws_settings.row_values(1)
        if not s_first:
            self.ws_settings.append_row(SETTINGS_HEADERS, value_input_option="USER_ENTERED")
            for row in DEFAULT_SETTINGS:
                self.ws_settings.append_row(row, value_input_option="USER_ENTERED")

    async def init_db(self):
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)

    def _get_settings_sync(self) -> dict[str, str]:
        rows = self.ws_settings.get_all_values()
        settings = {k: v for k, v in DEFAULT_SETTINGS}
        for row in rows[1:]:
            if len(row) >= 2 and row[0].strip():
                settings[row[0].strip()] = row[1].strip()
        return settings

    async def get_settings(self) -> dict[str, str]:
        async with self._lock:
            return await asyncio.to_thread(self._get_settings_sync)

    def _get_all_clients_sync(self) -> list[dict]:
        rows = self.ws_clients.get_all_values()
        if len(rows) <= 1:
            return []
        headers = rows[0]
        clients = []
        today = now_tz().date()

        for row_idx, row in enumerate(rows[1:], start=2):
            padded = row + [""] * (len(headers) - len(row))
            item = {headers[i]: padded[i] for i in range(len(headers))}
            item["_row_num"] = row_idx
            item["id_int"] = to_int(item.get("ID"))
            item["soni_int"] = to_int(item.get("Soni"))
            item["narx_int"] = to_int(item.get("Narx_1_Dona"))
            item["jami_int"] = to_int(item.get("Jami_Summa"))
            item["zaklad_int"] = to_int(item.get("Zaklad_Summa"))
            item["qoldiq_int"] = to_int(item.get("Qoldiq_Summa"))

            # Va'dalar JSON parse
            raw_v = item.get("Vadalar_JSON", "").strip()
            try:
                vadalar = json.loads(raw_v) if raw_v else []
                if not isinstance(vadalar, list):
                    vadalar = []
            except Exception:
                vadalar = []
            item["vadalar_list"] = vadalar

            # Kechikish kunlarini hisoblash
            last_dt = parse_date_safe(item.get("Oxirgi_Aloqa_Sana")) or parse_date_safe(
                item.get("Qoshilgan_Sana")
            )
            days_waiting = (today - last_dt).days if last_dt else 0
            item["days_waiting"] = max(0, days_waiting)

            status = item.get("Status", "Yangi").strip() or "Yangi"
            item["Status"] = status

            # Priority aniqlash:
            # 1: Emergency (2+ kun o'tgan va hali Zaklad_olindi/Yuklandi/Rad_etildi emas)
            # 2: Bugungi reja (Keyingi_Qongiroq_Vaqti bugun yoki bugun qo'shilgan)
            # 3: Boshqa faol (Yangi / Muzokara)
            # 4: Zaklad tushganlar (Zaklad_olindi / Yuklandi)
            # 5: Rad etilganlar
            next_call_dt = parse_datetime_safe(item.get("Keyingi_Qongiroq_Vaqti"))
            created_dt = parse_date_safe(item.get("Qoshilgan_Sana"))

            is_emergency = (
                status not in ("Zaklad_olindi", "Yuklandi", "Rad_etildi")
                and item["days_waiting"] >= 2
            )
            is_today_plan = (
                status not in ("Zaklad_olindi", "Yuklandi", "Rad_etildi")
                and (
                    (next_call_dt is not None and next_call_dt.date() <= today)
                    or (created_dt == today)
                )
            )

            item["is_emergency"] = is_emergency
            item["is_today_plan"] = is_today_plan

            if is_emergency:
                item["priority_group"] = 1
            elif is_today_plan:
                item["priority_group"] = 2
            elif status in ("Yangi", "Muzokara"):
                item["priority_group"] = 3
            elif status in ("Zaklad_olindi", "Yuklandi"):
                item["priority_group"] = 4
            else:
                item["priority_group"] = 5

            clients.append(item)

        # Aqlli saralash: Priority guruh bo'yicha, so'ngra ko'proq kutganlar tepada
        clients.sort(key=lambda x: (x["priority_group"], -x["days_waiting"], x["id_int"]))
        return clients

    async def get_all_clients(self) -> list[dict]:
        async with self._lock:
            return await asyncio.to_thread(self._get_all_clients_sync)

    async def get_client_by_id(self, client_id: int) -> dict | None:
        clients = await self.get_all_clients()
        for c in clients:
            if c["id_int"] == client_id:
                return c
        return None

    def _add_client_sync(self, data: dict) -> int:
        rows = self.ws_clients.get_all_values()
        max_id = 0
        for row in rows[1:]:
            if row and row[0].isdigit():
                max_id = max(max_id, int(row[0]))
        new_id = max_id + 1
        today = today_str()

        soni = to_int(data.get("Soni", 0))
        narx = to_int(data.get("Narx_1_Dona", 0))
        jami = soni * narx if (soni and narx) else to_int(data.get("Jami_Summa", 0))
        zaklad = to_int(data.get("Zaklad_Summa", 0))
        qoldiq = max(0, jami - zaklad)

        row_map = {
            "ID": str(new_id),
            "Qoshilgan_Sana": today,
            "Oxirgi_Aloqa_Sana": today,
            "Ism": data.get("Ism", "Noma'lum").strip(),
            "Telefon": data.get("Telefon", "").strip(),
            "TG_Username": data.get("TG_Username", "").replace("@", "").strip(),
            "Viloyat_Tuman": data.get("Viloyat_Tuman", "").strip(),
            "Nav": data.get("Nav", "Atlant").strip(),
            "Soni": str(soni),
            "Narx_1_Dona": str(narx),
            "Jami_Summa": str(jami),
            "Zaklad_Summa": str(zaklad),
            "Qoldiq_Summa": str(qoldiq),
            "Paket_Ogit_Info": data.get("Paket_Ogit_Info", "").strip(),
            "Vadalar_JSON": json.dumps(data.get("Vadalar_JSON", []), ensure_ascii=False),
            "Qongiroq_1": data.get("Qongiroq_1", ""),
            "Qongiroq_2": data.get("Qongiroq_2", ""),
            "Qongiroq_3": data.get("Qongiroq_3", ""),
            "Qongiroq_4": data.get("Qongiroq_4", ""),
            "Status": data.get("Status", "Yangi"),
            "Keyingi_Qongiroq_Vaqti": data.get("Keyingi_Qongiroq_Vaqti", f"{today} 18:00"),
        }
        new_row = [row_map.get(h, "") for h in CLIENTS_HEADERS]
        self.ws_clients.append_row(new_row, value_input_option="USER_ENTERED")
        return new_id

    async def add_client(self, data: dict) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._add_client_sync, data)

    def _update_client_sync(self, client_id: int, updates: dict) -> bool:
        rows = self.ws_clients.get_all_values()
        if len(rows) <= 1:
            return False
        headers = rows[0]
        target_row_num = None
        current_row = None

        for idx, row in enumerate(rows[1:], start=2):
            if row and to_int(row[0]) == client_id:
                target_row_num = idx
                current_row = row + [""] * (len(headers) - len(row))
                break

        if not target_row_num:
            return False

        row_dict = {headers[i]: current_row[i] for i in range(len(headers))}
        for k, v in updates.items():
            if k in row_dict:
                if k == "Vadalar_JSON" and isinstance(v, list):
                    row_dict[k] = json.dumps(v, ensure_ascii=False)
                elif k == "TG_Username":
                    row_dict[k] = str(v).replace("@", "").strip()
                else:
                    row_dict[k] = str(v)

        # Moliyaviy maydonlarni avtomatik qayta hisoblash
        soni = to_int(row_dict.get("Soni"))
        narx = to_int(row_dict.get("Narx_1_Dona"))
        if soni > 0 and narx > 0:
            jami = soni * narx
            row_dict["Jami_Summa"] = str(jami)
        else:
            jami = to_int(row_dict.get("Jami_Summa"))

        zaklad = to_int(row_dict.get("Zaklad_Summa"))
        qoldiq = max(0, jami - zaklad)
        row_dict["Qoldiq_Summa"] = str(qoldiq)

        # Agar zaklad tushgan bo'lsa va status hali Yangi/Muzokara bo'lsa -> Zaklad_olindi
        if zaklad > 0 and row_dict.get("Status") in ("Yangi", "Muzokara", ""):
            if "Status" not in updates:
                row_dict["Status"] = "Zaklad_olindi"

        updated_list = [row_dict.get(h, "") for h in headers]
        end_col_letter = chr(64 + len(headers))
        self.ws_clients.update(
            f"A{target_row_num}:{end_col_letter}{target_row_num}",
            [updated_list],
            value_input_option="USER_ENTERED",
        )
        return True

    async def update_client(self, client_id: int, updates: dict) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._update_client_sync, client_id, updates)

    async def get_dashboard_stats(self) -> dict:
        clients = await self.get_all_clients()
        settings = await self.get_settings()
        today = today_str()

        emergency_list = [c for c in clients if c["is_emergency"]]
        zaklad_list = [c for c in clients if c["Status"] in ("Zaklad_olindi", "Yuklandi")]

        # Bugungi qo'ng'iroqlar progressi
        today_targets = [
            c
            for c in clients
            if c["is_emergency"]
            or c["is_today_plan"]
            or c.get("Oxirgi_Aloqa_Sana", "").startswith(today)
        ]
        done_today = [
            c
            for c in today_targets
            if c.get("Oxirgi_Aloqa_Sana", "").startswith(today)
            and (c.get("Qongiroq_1") or c.get("Qongiroq_2") or c.get("Qongiroq_3") or c.get("Qongiroq_4"))
        ]

        total_today = max(len(today_targets), 1)
        done_count = len(done_today)
        pct = min(100, int((done_count / total_today) * 100)) if today_targets else 0

        # Oylik maqsad (zaklad olingan yoki yuklangan ko'chatlar soni)
        sold_seedlings = sum(
            c["soni_int"] for c in clients if c["Status"] in ("Zaklad_olindi", "Yuklandi")
        )
        monthly_goal = to_int(settings.get("OYLIK_KOCHAT_MAQSAD", 50000)) or 50000

        total_zaklad = sum(c["zaklad_int"] for c in clients)
        total_qoldiq = sum(
            c["qoldiq_int"] for c in clients if c["Status"] in ("Zaklad_olindi", "Muzokara", "Yangi")
        )

        return {
            "total_clients": len(clients),
            "emergency_count": len(emergency_list),
            "zaklad_count": len(zaklad_list),
            "done_today": done_count,
            "total_today": len(today_targets),
            "today_pct": pct,
            "sold_seedlings": sold_seedlings,
            "monthly_goal": monthly_goal,
            "total_zaklad": total_zaklad,
            "total_qoldiq": total_qoldiq,
            "settings": settings,
            "clients": clients,
        }