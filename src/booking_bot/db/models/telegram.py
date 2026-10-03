from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from booking_bot.db.base import Base


class TelegramUpdateReceipt(Base):
    __tablename__ = "telegram_update_receipts"

    namespace: Mapped[str] = mapped_column(String(128), primary_key=True)
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
