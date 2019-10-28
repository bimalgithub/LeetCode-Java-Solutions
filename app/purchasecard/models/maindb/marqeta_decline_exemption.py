from dataclasses import dataclass

from sqlalchemy import Column, Integer, DateTime, Text
from typing_extensions import final

from app.commons.database.model import TableDefinition
from app.commons.utils.dataclass_extensions import no_init_field


@final
@dataclass(frozen=True)
class MarqetaDeclineExemptionTable(TableDefinition):
    name: str = no_init_field("marqeta_decline_exemption")
    id: Column = no_init_field(Column("id", Integer, primary_key=True))
    amount: Column = no_init_field(Column("amount", Integer))
    mid: Column = no_init_field(Column("mid", Text))
    delivery_id: Column = no_init_field(Column("delivery_id", Integer))
    created_at: Column = no_init_field(Column("created_at", DateTime(True)))
    used_at: Column = no_init_field(Column("used_at", DateTime(True)))
    dasher_id: Column = no_init_field(Column("dasher_id", Integer))
    created_by_id: Column = no_init_field(Column("created_by_id", Integer))
