import os
import json
import asyncio
from datetime import datetime
import pytz
from groq import Groq

TZ = pytz.timezone("Asia/Tashkent")


class VoiceCRMProcessor:
    def __init__(self):
        self.api_key = os.getenv("GROQ_API_KEY", "")
        self.client = Groq(api_key=self.api_key) if self.api_key else None

    def _transcribe_and_extract_sync(self, audio_path: str, client_context: dict) -> dict:
        if not self.client:
            raise RuntimeError("GROQ_API_KEY topilmadi!")

        # 1. Whisper-large-v3 orqali ovozni matnga o'girish
        with open(audio_path, "rb") as file:
            transcription = self.client.audio.transcriptions.create(
                file=(os.path.basename(audio_path), file.read()),
                model="whisper-large-v3",
                response_format="text",
                language="uz",
            )

        transcript_text = str(transcription).strip()
        now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

        # 2. Llama-3.3-70b-versatile orqali CRM ma'lumotlarini JSON formatda ajratish
        system_prompt = f"""
Sen "Malinachi_" (Angren ko'chatxonasi: Atlant va Maravilla malina ko'chatlari) agrobiznesi uchun aqlli CRM tahlilchisisisan.
Hozirgi aniq vaqt (Toshkent): {now_str}.

Mijozning joriy ma'lumotlari:
- Ism: {client_context.get('Ism')}
- Viloyat/Tuman: {client_context.get('Viloyat_Tuman')}
- Nav: {client_context.get('Nav')}
- Soni: {client_context.get('Soni')}
- Narx (1 dona): {client_context.get('Narx_1_Dona')}
- Zaklad: {client_context.get('Zaklad_Summa')}
- Paket/O'g'it: {client_context.get('Paket_Ogit_Info')}
- Status: {client_context.get('Status')}

Vazifa: Sotuv menejerining ovozli xabaridan (transkripsiyadan) quyidagi maydonlarni aniq JSON formatda ajratib ber.
Faqat quyidagi kalitlarga ega valid JSON qaytar (boshqa hech qanday matn yozma):
{{
  "call_summary": "Qo'ng'iroqning qisqa va aniq xulosasi (1-2 gap, o'zbek tilida)",
  "nav": "Atlant yoki Maravilla yoki ikkalasi (agar aytilgan bo'lsa, aks holda null)",
  "soni": integer yoki null (ko'chat soni),
  "narx_1_dona": integer yoki null (so'mda 1 dona ko'chat narxi, masalan 22000),
  "zaklad_summa": integer yoki null (so'mda olingan/kelishilgan zaklad summasi),
  "paket_ogit_info": "string yoki null (go'ng, gumus, o'g'it paketi bo'yicha kelishuv)",
  "yangi_vadalar": ["string", "..."] (mijozga berilgan yangi va'dalar ro'yxati, masalan 'Video yuborish', 'Hisob-kitob tashlash', bo'lmasa []),
  "status": "Yangi" | "Muzokara" | "Zaklad_olindi" | "Yuklandi" | "Rad_etildi" | null,
  "keyingi_qongiroq_vaqti": "YYYY-MM-DD HH:MM" formatida keyingi aloqa vaqti (masalan 'ertaga soat 10 da' desa ertangi sana 10:00, aytilmagan bo'lsa null)
}}
"""

        completion = self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Ovozli xabar matni:\n{transcript_text}"},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        raw_json = completion.choices[0].message.content
        parsed = json.loads(raw_json)
        parsed["transcript"] = transcript_text
        return parsed

    async def process_voice(self, audio_path: str, client_context: dict) -> dict:
        return await asyncio.to_thread(
            self._transcribe_and_extract_sync, audio_path, client_context
        )