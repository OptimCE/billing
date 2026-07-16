from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.database.database import CrmBase


class AppUserModel(CrmBase):
    # Partial mapping of the CRM `app_user` table: only the columns the audit
    # log service needs to denormalise the writer's identity onto each row.
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    auth_user_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(256), nullable=False)
