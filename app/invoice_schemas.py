from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum


class DecisionEnum(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    FLAG_FOR_REVIEW = "FLAG_FOR_REVIEW"
    REJECT = "REJECT"


class ReviewerActionEnum(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_INFO = "REQUEST_INFO"
    OVERRIDE_MATCH = "OVERRIDE_MATCH"


class LineItem(BaseModel):
    line_number: int = Field(default=1, description="Line item sequence number")
    description: str = Field(..., description="Description of the goods or services")
    quantity: float = Field(..., ge=0, description="Quantity invoiced")
    unit_price: float = Field(..., ge=0, description="Unit price per item")
    total_price: float = Field(..., description="Line total price")


class PurchaseOrder(BaseModel):
    po_number: str = Field(..., description="Purchase Order identifier")
    vendor_name: str = Field(..., description="Vendor legal entity name")
    currency: str = Field(default="USD", description="Currency ISO code")
    status: str = Field(default="OPEN", description="PO lifecycle status: OPEN, CLOSED, CANCELLED")
    line_items: List[LineItem] = Field(default_factory=list, description="Contracted line items")
    subtotal: float = Field(default=0.0, ge=0)
    tax_amount: float = Field(default=0.0, ge=0)
    total_amount: float = Field(..., ge=0)
    created_at: Optional[str] = None


class InvoiceData(BaseModel):
    invoice_number: str = Field(..., description="Invoice identifier")
    vendor_name: str = Field(..., description="Vendor company name")
    vendor_tax_id: Optional[str] = None
    invoice_date: Optional[str] = None
    due_date: Optional[str] = None
    po_number: Optional[str] = None
    currency: str = Field(default="USD")
    line_items: List[LineItem] = Field(default_factory=list)
    subtotal: float = Field(default=0.0)
    tax_amount: float = Field(default=0.0)
    total_amount: float = Field(default=0.0)
    notes: Optional[str] = None
    raw_text: Optional[str] = None
    extraction_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    extraction_warnings: List[str] = Field(default_factory=list)


class ReviewerActionRequest(BaseModel):
    reviewer_id: str = Field(..., min_length=2, description="Identity of the reviewing AP analyst")
    action: str = Field(..., description="APPROVE, REJECT, REQUEST_INFO, or OVERRIDE_MATCH")
    rationale: str = Field(..., min_length=3, description="Mandatory audit justification for the action")
    notes: Optional[str] = None


class AuditEntry(BaseModel):
    action_id: str
    timestamp: str
    reviewer_id: str
    action: str
    previous_status: str
    new_status: str
    rationale: str
    metadata: Optional[Dict[str, Any]] = None


class InvoiceEvaluationResult(BaseModel):
    invoice_id: str
    invoice_number: str
    vendor_name: str
    po_number: Optional[str] = None
    currency: str = "USD"
    total_amount: float
    system_decision: str
    final_status: str
    risk_level: str
    confidence_score: float
    checks: Dict[str, Any]
    discrepancies: List[str]
    rule_breakdown: List[Dict[str, Any]]
    audit_trail: List[AuditEntry]
    invoice_data: Dict[str, Any]
    po_data: Optional[Dict[str, Any]] = None
    evaluated_at: str
    payment_disclaimer: str = "ELIGIBLE FOR NEXT AP STEP (PAYMENT HAS NOT OCCURRED)"


class ToleranceConfig(BaseModel):
    rounding_tolerance: float = 0.05
    price_variance_tolerance_pct: float = 0.0
    amount_tolerance: float = 0.05
