from typing import Optional, TypedDict


class SupervisorState(TypedDict, total=False):
    """Shared state threaded through the CSV/PDF supervisor graph."""
    query: str
    csv_text: Optional[str]
    pdf_text: Optional[str]
    csv_answer: Optional[str]
    pdf_answer: Optional[str]
    use_db: bool
    db_answer: Optional[str]
    db_sql: Optional[str]
    db_rows: int
    news_answer: Optional[str]
    news_symbol: Optional[str]
    final_answer: str
