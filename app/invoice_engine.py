import io
import csv
import re
import json
import uuid
import logging
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from app.invoice_schemas import (
    InvoiceData, LineItem, PurchaseOrder,
    InvoiceEvaluationResult, AuditEntry, ReviewerActionRequest,
    DecisionEnum
)

# Third-party PDF parser
try:
    import pypdf
except ImportError:
    pypdf = None

logger = logging.getLogger("flowpilot.invoice")
logger.setLevel(logging.INFO)

# Default tolerances
DEFAULT_ROUNDING_TOLERANCE = Decimal("0.05")
DEFAULT_PRICE_TOLERANCE_PCT = Decimal("0.00")

# Maximum upload size (10 MB)
MAX_INVOICE_FILE_SIZE = 10 * 1024 * 1024


def to_decimal(val: Any) -> Decimal:
    """Safely converts float, int, str to Decimal with 2 decimal places."""
    if val is None:
        return Decimal("0.00")
    try:
        d = Decimal(str(val))
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


class InvoiceProcessingEngine:
    """
    Thread-safe, explainable AP Invoice Processing Engine.
    Handles extraction, 2-way PO matching, financial validation, duplicate detection,
    and reviewer audit lifecycle with Decimal arithmetic precision.
    """

    def __init__(self, po_file_path: Optional[Path] = None):
        self.lock = threading.Lock()
        self.purchase_orders: Dict[str, Dict[str, Any]] = {}
        self.evaluated_invoices: Dict[str, Dict[str, Any]] = {}
        # Duplicate detection registry: stores set of (vendor_normalized, invoice_num_normalized) -> invoice_id
        self.duplicate_registry: Dict[Tuple[str, str], str] = {}
        # Fingerprint registry: (vendor_norm, invoice_num_norm, total_amount_str) -> invoice_id
        self.fingerprint_registry: Dict[Tuple[str, str, str], str] = {}
        self.rounding_tolerance = DEFAULT_ROUNDING_TOLERANCE

        if po_file_path and po_file_path.exists():
            self.load_purchase_orders_from_file(po_file_path)

    def load_purchase_orders_from_file(self, path: Path):
        with self.lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for po in data:
                    po_num = po.get("po_number", "").strip().upper()
                    if po_num:
                        self.purchase_orders[po_num] = po
                logger.info(f"Loaded {len(self.purchase_orders)} synthetic purchase orders from {path.name}")
            except Exception as e:
                logger.error(f"Failed to load purchase orders from {path}: {e}")

    def add_purchase_order(self, po: PurchaseOrder) -> Dict[str, Any]:
        with self.lock:
            po_dict = po.model_dump()
            po_num = po_dict["po_number"].strip().upper()
            self.purchase_orders[po_num] = po_dict
            return po_dict

    def get_purchase_order(self, po_number: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self.purchase_orders.get(po_number.strip().upper())

    def list_purchase_orders(self) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.purchase_orders.values())

    def reset_state(self):
        """Resets evaluated invoices and duplicate registry for test isolation."""
        with self.lock:
            self.evaluated_invoices.clear()
            self.duplicate_registry.clear()
            self.fingerprint_registry.clear()
            logger.info("Invoice processing engine state reset to empty.")

    # ------------------------------------------------------------------------
    # Extraction Helpers
    # ------------------------------------------------------------------------
    def extract_from_pdf_bytes(self, raw_bytes: bytes, filename: str) -> InvoiceData:
        """
        Safely extracts invoice metadata and line items from PDF bytes using pypdf.
        Handles scanned/empty PDFs safely without crashing and flags low confidence.
        NEVER invents missing invoice values.
        """
        if len(raw_bytes) > MAX_INVOICE_FILE_SIZE:
            raise ValueError(f"File size {len(raw_bytes)} bytes exceeds maximum limit of 10MB.")

        if not pypdf:
            raise RuntimeError("pypdf is required for PDF invoice extraction.")

        try:
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        except Exception as e:
            return InvoiceData(
                invoice_number=f"MALFORMED-{uuid.uuid4().hex[:6].upper()}",
                vendor_name="Unknown Vendor",
                extraction_confidence=0.0,
                extraction_warnings=[f"Malformed or unreadable PDF document: {str(e)}"],
                raw_text=""
            )

        extracted_text = ""
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
                extracted_text += page_text + "\n"
            except Exception:
                pass

        cleaned_text = extracted_text.strip()
        if not cleaned_text:
            # Scanned or non-selectable text
            return InvoiceData(
                invoice_number=f"SCANNED-{uuid.uuid4().hex[:6].upper()}",
                vendor_name="Unidentified (Scanned Document)",
                extraction_confidence=0.0,
                extraction_warnings=[
                    "Document appears to be scanned or contains no selectable text. Optical Character Recognition (OCR) or manual analyst entry required."
                ],
                raw_text=""
            )

        return self.parse_text_to_invoice(cleaned_text, source_name=filename)

    def parse_text_to_invoice(self, text: str, source_name: str = "") -> InvoiceData:
        """
        Heuristic parsing for commercial and service invoices.
        Extracts invoice number, vendor, PO number, dates, line items, and totals.
        Never fabricates missing values.
        """
        warnings: List[str] = []
        confidence = 1.0

        # Invoice Number
        inv_match = re.search(
            r'(?:invoice\s*(?:number|no\.?|#)\s*[:.]?|inv\s*(?:no\.?|#|number)\s*[:.]?)\s*([A-Za-z0-9\-_]+)',
            text, re.I
        )
        invoice_number = inv_match.group(1).strip() if inv_match else None
        if not invoice_number:
            # Fallback check for "Invoice: INV-..."
            fallback = re.search(r'(?:^|\n)\s*invoice\s*[:]\s*([A-Za-z0-9\-_]+)', text, re.I)
            if fallback:
                invoice_number = fallback.group(1).strip()

        if not invoice_number:
            invoice_number = f"INV-UNKNOWN-{uuid.uuid4().hex[:6].upper()}"
            warnings.append("Missing invoice number in document; flagged for verification.")
            confidence -= 0.3

        # Vendor Name
        vendor_match = re.search(r'(?:^|\n)\s*vendor\s*[:.]?\s*([^\n\r]+)', text, re.I)
        vendor_name = vendor_match.group(1).strip() if vendor_match else None
        if not vendor_name:
            # Look for typical company markers in lines
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for line in lines[:5]:
                if any(suffix in line.lower() for suffix in ['inc', 'ltd', 'llc', 'corp', 'systems', 'services', 'supplies', 'labs']):
                    vendor_name = line
                    break
        if not vendor_name:
            vendor_name = "Unspecified Vendor"
            warnings.append("Could not identify vendor name; human review required.")
            confidence -= 0.3

        # PO Number
        po_match = re.search(r'(?:p\.?o\.?\s*(?:number|no\.?|#)\s*[:.]?|purchase\s*order\s*(?:#|no\.?|number)\s*[:.]?)\s*([A-Za-z0-9\-_]+)', text, re.I)
        po_number = po_match.group(1).strip().upper() if po_match else None
        if not po_number:
            warnings.append("No Purchase Order number referenced.")

        # Dates
        date_match = re.search(r'(?:invoice\s*date|date)\s*[:.]?\s*(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})', text, re.I)
        invoice_date = date_match.group(1).strip() if date_match else None

        due_date_match = re.search(r'due\s*date\s*[:.]?\s*(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})', text, re.I)
        due_date = due_date_match.group(1).strip() if due_date_match else None

        # Currency
        currency = "USD"
        curr_match = re.search(r'currency\s*[:.]?\s*([A-Za-z]{3})', text, re.I)
        if curr_match:
            currency = curr_match.group(1).strip().upper()
        elif "eur" in text.lower() or "€" in text:
            currency = "EUR"
        elif "gbp" in text.lower() or "£" in text:
            currency = "GBP"
        elif "inr" in text.lower() or "₹" in text:
            currency = "INR"

        # Line items
        # Pattern example: Line Item 1: Cloud Compute Node | Qty: 2 | Unit Price: 750.00 | Total: 1500.00
        line_items: List[LineItem] = []
        line_item_regex = re.compile(
            r'(?:line\s*item\s*(\d+)\s*[:.]?\s*)?([^|\n]+?)\s*\|\s*qty\s*[:.]?\s*([\d.]+)\s*\|\s*unit\s*price\s*[:.]?\s*([\d.]+)\s*\|\s*total\s*[:.]?\s*([\d.]+)',
            re.IGNORECASE
        )
        for idx, match in enumerate(line_item_regex.finditer(text)):
            line_no = int(match.group(1)) if match.group(1) else (idx + 1)
            desc = match.group(2).strip()
            qty = float(match.group(3))
            unit_p = float(match.group(4))
            tot_p = float(match.group(5))
            line_items.append(LineItem(
                line_number=line_no,
                description=desc,
                quantity=qty,
                unit_price=unit_p,
                total_price=tot_p
            ))

        # Financial totals (anchored to line starts so we don't accidentally match line-item totals)
        subtotal_match = re.search(r'(?:^|\n)\s*(?:subtotal|sub-total)\s*[:.]?\s*([\d,]+\.?\d*)', text, re.I)
        tax_match = re.search(r'(?:^|\n)\s*(?:tax|vat|gst)\s*[:.]?\s*([\d,]+\.?\d*)', text, re.I)
        total_match = re.search(r'(?:^|\n)\s*(?:total\s*amount|grand\s*total|amount\s*due|total)\s*[:.]?\s*([\d,]+\.?\d*)', text, re.I)

        subtotal = float(subtotal_match.group(1).replace(',', '')) if subtotal_match else 0.0
        tax_amount = float(tax_match.group(1).replace(',', '')) if tax_match else 0.0
        total_amount = float(total_match.group(1).replace(',', '')) if total_match else 0.0

        # If subtotal wasn't explicitly stated but line items exist, sum line items
        if subtotal == 0.0 and line_items:
            subtotal = sum(item.total_price for item in line_items)

        if total_amount == 0.0:
            if subtotal > 0:
                total_amount = subtotal + tax_amount
            else:
                warnings.append("Could not extract invoice total amount.")
                confidence -= 0.4

        confidence = max(0.0, min(1.0, round(confidence, 2)))

        return InvoiceData(
            invoice_number=invoice_number,
            vendor_name=vendor_name,
            invoice_date=invoice_date,
            due_date=due_date,
            po_number=po_number,
            currency=currency,
            line_items=line_items,
            subtotal=subtotal,
            tax_amount=tax_amount,
            total_amount=total_amount,
            raw_text=text[:2000],  # truncated snippet
            extraction_confidence=confidence,
            extraction_warnings=warnings
        )

    # ------------------------------------------------------------------------
    # Core Decision & Validation Engine
    # ------------------------------------------------------------------------
    def evaluate_invoice(self, invoice: InvoiceData) -> InvoiceEvaluationResult:
        """
        Evaluates an invoice with strict policy rules:
          1. Security / Injection / Malformed / Scanned Check
          2. Duplicate Invoice Detection (Concurrency-Safe)
          3. Financial Arithmetic & Tax Verification (Decimal Precision)
          4. Purchase Order Matching (2-Way Match & Status Verification)
          5. Policy Decision Synthesis: AUTO_APPROVE, FLAG_FOR_REVIEW, REJECT
        """
        invoice_id = f"INV-EVAL-{uuid.uuid4().hex[:8].upper()}"
        now_iso = datetime.now(timezone.utc).isoformat()

        checks: Dict[str, Any] = {}
        discrepancies: List[str] = []
        rule_breakdown: List[Dict[str, Any]] = []

        # System decision tracking
        decision = DecisionEnum.AUTO_APPROVE.value
        risk_level = "LOW"
        confidence = invoice.extraction_confidence

        # --------------------------------------------------------------------
        # Rule 1: Extraction & Security / Injection Safety
        # --------------------------------------------------------------------
        text_for_security = f"{invoice.vendor_name} {invoice.notes or ''} {invoice.raw_text or ''}".lower()
        has_injection = any(phrase in text_for_security for phrase in [
            'ignore previous instructions', 'system override', 'jailbreak',
            'auto approve this invoice', 'bypass verification', 'disregard policy',
            'unrestricted mode'
        ])

        if has_injection:
            decision = DecisionEnum.REJECT.value
            risk_level = "HIGH"
            discrepancies.append("Adversarial prompt injection pattern detected in invoice metadata.")
            checks["security_check"] = {
                "passed": False,
                "status": "FAIL",
                "message": "Prompt injection detected in invoice payload."
            }
            rule_breakdown.append({
                "rule": "SECURITY_AND_INTEGRITY",
                "result": "FAIL",
                "details": "Payload contains adversarial instructions attempting policy subversion."
            })
        else:
            checks["security_check"] = {
                "passed": True,
                "status": "PASS",
                "message": "No security threats or injection patterns detected."
            }

        # Check extraction confidence / unreadable document
        if invoice.extraction_confidence < 0.5 or not invoice.total_amount or invoice.extraction_warnings:
            if decision != DecisionEnum.REJECT.value:
                decision = DecisionEnum.FLAG_FOR_REVIEW.value
                risk_level = "MEDIUM" if risk_level == "LOW" else risk_level
            for w in invoice.extraction_warnings:
                discrepancies.append(f"Extraction warning: {w}")
            checks["extraction_quality"] = {
                "passed": invoice.extraction_confidence >= 0.7,
                "status": "WARNING" if invoice.extraction_confidence < 0.7 else "PASS",
                "confidence": invoice.extraction_confidence,
                "warnings": invoice.extraction_warnings
            }
        else:
            checks["extraction_quality"] = {
                "passed": True,
                "status": "PASS",
                "confidence": invoice.extraction_confidence,
                "warnings": []
            }

        # --------------------------------------------------------------------
        # Rule 2: Concurrency-Safe Duplicate Detection
        # --------------------------------------------------------------------
        vendor_norm = invoice.vendor_name.strip().lower()
        inv_num_norm = invoice.invoice_number.strip().upper()
        tot_str = f"{invoice.total_amount:.2f}"

        duplicate_key = (vendor_norm, inv_num_norm)
        fingerprint_key = (vendor_norm, inv_num_norm, tot_str)

        is_duplicate = False
        duplicate_ref = None

        with self.lock:
            if duplicate_key in self.duplicate_registry:
                is_duplicate = True
                duplicate_ref = self.duplicate_registry[duplicate_key]
            else:
                # Register under lock to prevent concurrent races
                self.duplicate_registry[duplicate_key] = invoice_id
                self.fingerprint_registry[fingerprint_key] = invoice_id

        if is_duplicate:
            decision = DecisionEnum.REJECT.value
            risk_level = "HIGH"
            discrepancy_msg = f"Duplicate invoice detected: '{invoice.invoice_number}' from '{invoice.vendor_name}' was previously processed (ref: {duplicate_ref})."
            discrepancies.append(discrepancy_msg)
            checks["duplicate_check"] = {
                "passed": False,
                "status": "FAIL",
                "existing_invoice_id": duplicate_ref,
                "message": discrepancy_msg
            }
            rule_breakdown.append({
                "rule": "DUPLICATE_DETECTION",
                "result": "FAIL",
                "details": discrepancy_msg
            })
        else:
            checks["duplicate_check"] = {
                "passed": True,
                "status": "PASS",
                "message": "Unique invoice identifier and vendor fingerprint verified."
            }

        # --------------------------------------------------------------------
        # Rule 3: Decimal Financial Arithmetic & Tax Verification
        # --------------------------------------------------------------------
        dec_subtotal = to_decimal(invoice.subtotal)
        dec_tax = to_decimal(invoice.tax_amount)
        dec_total = to_decimal(invoice.total_amount)

        # If subtotal was not explicitly provided but total exists and no line items are defined:
        if dec_subtotal == Decimal("0.00") and not invoice.line_items and dec_total > Decimal("0.00"):
            dec_subtotal = dec_total - dec_tax

        arithmetic_ok = True
        arithmetic_errors = []

        if dec_total <= Decimal("0.00"):
            if invoice.extraction_confidence < 0.5:
                # Missing/uncertain value due to scanned or unreadable document -> route to human review
                if decision != DecisionEnum.REJECT.value:
                    decision = DecisionEnum.FLAG_FOR_REVIEW.value
                    risk_level = "HIGH"
                discrepancies.append("Uncertain extraction: invoice total could not be determined. Requires human review.")
                arithmetic_ok = False
                arithmetic_errors.append("Unextracted total amount.")
            else:
                decision = DecisionEnum.REJECT.value
                risk_level = "HIGH"
                discrepancies.append(f"Invoice total amount must be strictly greater than 0. Received: {dec_total}.")
                arithmetic_ok = False
                arithmetic_errors.append("Non-positive total amount.")

        # Check line item calculations
        computed_subtotal = Decimal("0.00")
        line_item_math_ok = True
        for item in invoice.line_items:
            qty_dec = to_decimal(item.quantity)
            price_dec = to_decimal(item.unit_price)
            tot_dec = to_decimal(item.total_price)
            calc_line = (qty_dec * price_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            
            line_diff = abs(calc_line - tot_dec)
            if line_diff > self.rounding_tolerance:
                line_item_math_ok = False
                err = f"Line {item.line_number} math variance: {qty_dec} x {price_dec} = {calc_line}, but stated total is {tot_dec}."
                arithmetic_errors.append(err)
            computed_subtotal += tot_dec

        # Check subtotal matching sum of line items if line items present
        if invoice.line_items:
            subtotal_diff = abs(computed_subtotal - dec_subtotal)
            if subtotal_diff > self.rounding_tolerance:
                line_item_math_ok = False
                err = f"Sum of line items ({computed_subtotal}) does not match invoice subtotal ({dec_subtotal})."
                arithmetic_errors.append(err)

        # Check tax + subtotal == total
        calc_total = dec_subtotal + dec_tax
        tax_total_diff = abs(calc_total - dec_total)
        if tax_total_diff > self.rounding_tolerance:
            arithmetic_ok = False
            err = f"Financial arithmetic mismatch: Subtotal ({dec_subtotal}) + Tax ({dec_tax}) = {calc_total}, but stated Total is {dec_total} (Difference: {tax_total_diff})."
            arithmetic_errors.append(err)

        if not arithmetic_ok or not line_item_math_ok:
            if decision != DecisionEnum.REJECT.value:
                decision = DecisionEnum.FLAG_FOR_REVIEW.value
                risk_level = "HIGH" if not arithmetic_ok else "MEDIUM"
            for err in arithmetic_errors:
                discrepancies.append(err)
            checks["arithmetic_check"] = {
                "passed": False,
                "status": "FAIL",
                "stated_subtotal": float(dec_subtotal),
                "stated_tax": float(dec_tax),
                "stated_total": float(dec_total),
                "calculated_total": float(calc_total),
                "errors": arithmetic_errors
            }
            rule_breakdown.append({
                "rule": "ARITHMETIC_INTEGRITY",
                "result": "FAIL",
                "details": "; ".join(arithmetic_errors)
            })
        else:
            checks["arithmetic_check"] = {
                "passed": True,
                "status": "PASS",
                "stated_subtotal": float(dec_subtotal),
                "stated_tax": float(dec_tax),
                "stated_total": float(dec_total),
                "calculated_total": float(calc_total),
                "errors": []
            }

        # --------------------------------------------------------------------
        # Rule 4: Purchase Order Matching (2-Way & Lifecycle Verification)
        # --------------------------------------------------------------------
        po_data = None
        po_match_ok = True
        po_errors = []

        if not invoice.po_number:
            if decision != DecisionEnum.REJECT.value:
                decision = DecisionEnum.FLAG_FOR_REVIEW.value
                risk_level = "MEDIUM" if risk_level == "LOW" else risk_level
            discrepancies.append("No Purchase Order referenced. Non-PO direct invoice requires analyst approval.")
            po_match_ok = False
            checks["po_match_check"] = {
                "passed": False,
                "status": "MISSING_PO",
                "message": "Direct invoice without Purchase Order reference."
            }
            rule_breakdown.append({
                "rule": "PO_REFERENCE",
                "result": "MISSING",
                "details": "No PO number provided on invoice."
            })
        else:
            po_data = self.get_purchase_order(invoice.po_number)
            if not po_data:
                if decision != DecisionEnum.REJECT.value:
                    decision = DecisionEnum.FLAG_FOR_REVIEW.value
                    risk_level = "HIGH" if risk_level != "HIGH" else risk_level
                err = f"Purchase order '{invoice.po_number}' does not exist in the PO ledger."
                po_errors.append(err)
                discrepancies.append(err)
                po_match_ok = False
                checks["po_match_check"] = {
                    "passed": False,
                    "status": "PO_NOT_FOUND",
                    "po_number": invoice.po_number,
                    "message": err
                }
                rule_breakdown.append({
                    "rule": "PO_VERIFICATION",
                    "result": "NOT_FOUND",
                    "details": err
                })
            else:
                # PO exists: verify status, vendor, currency, amounts
                po_status = po_data.get("status", "OPEN").upper()
                if po_status == "CANCELLED":
                    decision = DecisionEnum.REJECT.value
                    risk_level = "HIGH"
                    err = f"Purchase Order '{invoice.po_number}' is CANCELLED. Invoices against cancelled POs cannot be paid."
                    po_errors.append(err)
                    discrepancies.append(err)
                    po_match_ok = False
                elif po_status == "CLOSED":
                    if decision != DecisionEnum.REJECT.value:
                        decision = DecisionEnum.FLAG_FOR_REVIEW.value
                        risk_level = "HIGH" if risk_level != "HIGH" else risk_level
                    err = f"Purchase Order '{invoice.po_number}' is CLOSED in PO ledger. Additional billing requires supervisor review."
                    po_errors.append(err)
                    discrepancies.append(err)
                    po_match_ok = False

                # Vendor matching
                po_vendor = po_data.get("vendor_name", "").strip().lower()
                inv_vendor = invoice.vendor_name.strip().lower()
                # Check fuzzy or partial inclusion
                if po_vendor != inv_vendor and po_vendor not in inv_vendor and inv_vendor not in po_vendor:
                    if decision != DecisionEnum.REJECT.value:
                        decision = DecisionEnum.FLAG_FOR_REVIEW.value
                        risk_level = "HIGH"
                    err = f"Vendor mismatch: PO vendor is '{po_data.get('vendor_name')}', but invoice is from '{invoice.vendor_name}'."
                    po_errors.append(err)
                    discrepancies.append(err)
                    po_match_ok = False

                # Currency matching
                po_currency = po_data.get("currency", "USD").upper()
                if po_currency != invoice.currency.upper():
                    if decision != DecisionEnum.REJECT.value:
                        decision = DecisionEnum.FLAG_FOR_REVIEW.value
                        risk_level = "MEDIUM" if risk_level == "LOW" else risk_level
                    err = f"Currency mismatch: PO is denominated in {po_currency}, but invoice is in {invoice.currency.upper()}."
                    po_errors.append(err)
                    discrepancies.append(err)
                    po_match_ok = False

                # Total amount matching
                po_tot_dec = to_decimal(po_data.get("total_amount", 0.0))
                diff_amount = abs(po_tot_dec - dec_total)
                if diff_amount > self.rounding_tolerance:
                    if decision != DecisionEnum.REJECT.value:
                        decision = DecisionEnum.FLAG_FOR_REVIEW.value
                        risk_level = "HIGH" if diff_amount > Decimal("50.00") else "MEDIUM"
                    err = f"PO total amount mismatch: Contracted PO total is {po_tot_dec} {po_currency}, but invoiced total is {dec_total} {invoice.currency} (Variance: {diff_amount})."
                    po_errors.append(err)
                    discrepancies.append(err)
                    po_match_ok = False

                # Line item matching (if both have line items)
                po_line_items = po_data.get("line_items", [])
                if invoice.line_items and po_line_items:
                    for inv_item in invoice.line_items:
                        # Find matching line item by line_number or description
                        matched_po_line = next(
                            (p for p in po_line_items if p.get("line_number") == inv_item.line_number or p.get("description", "").lower() in inv_item.description.lower()),
                            None
                        )
                        if matched_po_line:
                            po_unit_p = to_decimal(matched_po_line.get("unit_price"))
                            inv_unit_p = to_decimal(inv_item.unit_price)
                            p_diff = abs(po_unit_p - inv_unit_p)
                            if p_diff > self.rounding_tolerance:
                                if decision != DecisionEnum.REJECT.value:
                                    decision = DecisionEnum.FLAG_FOR_REVIEW.value
                                    risk_level = "HIGH" if risk_level != "HIGH" else risk_level
                                err = f"Unit price variance on item '{inv_item.description}': Invoiced at {inv_unit_p}, PO contracted price is {po_unit_p}."
                                po_errors.append(err)
                                discrepancies.append(err)
                                po_match_ok = False

                            po_qty = to_decimal(matched_po_line.get("quantity"))
                            inv_qty = to_decimal(inv_item.quantity)
                            if inv_qty > po_qty:
                                if decision != DecisionEnum.REJECT.value:
                                    decision = DecisionEnum.FLAG_FOR_REVIEW.value
                                    risk_level = "HIGH" if risk_level != "HIGH" else risk_level
                                err = f"Quantity variance on item '{inv_item.description}': Invoiced quantity {inv_qty} exceeds PO authorized quantity {po_qty}."
                                po_errors.append(err)
                                discrepancies.append(err)
                                po_match_ok = False

                checks["po_match_check"] = {
                    "passed": po_match_ok,
                    "status": "PASS" if po_match_ok else "MISMATCH",
                    "po_number": invoice.po_number,
                    "po_status": po_data.get("status"),
                    "contracted_amount": po_data.get("total_amount"),
                    "errors": po_errors
                }
                rule_breakdown.append({
                    "rule": "PO_MATCH_AND_LIFECYCLE",
                    "result": "PASS" if po_match_ok else "MISMATCH",
                    "details": "All PO terms, pricing, and line totals matched successfully." if po_match_ok else "; ".join(po_errors)
                })

        # --------------------------------------------------------------------
        # Rule 5: Decision Synthesis & Audit Initialization
        # --------------------------------------------------------------------
        if decision == DecisionEnum.AUTO_APPROVE.value:
            risk_level = "LOW"
            rule_breakdown.append({
                "rule": "FINAL_EVALUATION",
                "result": "AUTO_APPROVE",
                "details": "Clean 2-way match, exact financial arithmetic, open PO, and zero duplicate signals. Eligible for AP staging."
            })
        elif decision == DecisionEnum.FLAG_FOR_REVIEW.value:
            rule_breakdown.append({
                "rule": "FINAL_EVALUATION",
                "result": "FLAG_FOR_REVIEW",
                "details": f"Discrepancies identified ({len(discrepancies)} issue(s)). Routed to AP analyst queue for human review."
            })
        else:
            rule_breakdown.append({
                "rule": "FINAL_EVALUATION",
                "result": "REJECT",
                "details": f"Strict rejection triggered due to severe policy violation: {'; '.join(discrepancies[:2])}."
            })

        # Initial audit entry
        system_audit = AuditEntry(
            action_id=f"AUD-{uuid.uuid4().hex[:6].upper()}",
            timestamp=now_iso,
            reviewer_id="FLOWPILOT_POLICY_ENGINE",
            action="SYSTEM_EVALUATION",
            previous_status="NEW",
            new_status=decision,
            rationale=f"Automated evaluation completed with decision {decision}. Risk: {risk_level}. Discrepancies: {len(discrepancies)}.",
            metadata={
                "risk_level": risk_level,
                "confidence": confidence,
                "discrepancies_count": len(discrepancies)
            }
        )

        res = InvoiceEvaluationResult(
            invoice_id=invoice_id,
            invoice_number=invoice.invoice_number,
            vendor_name=invoice.vendor_name,
            po_number=invoice.po_number,
            currency=invoice.currency,
            total_amount=invoice.total_amount,
            system_decision=decision,
            final_status=decision,  # At start, final status matches original decision
            risk_level=risk_level,
            confidence_score=confidence,
            checks=checks,
            discrepancies=discrepancies,
            rule_breakdown=rule_breakdown,
            audit_trail=[system_audit],
            invoice_data=invoice.model_dump(),
            po_data=po_data,
            evaluated_at=now_iso,
            payment_disclaimer="ELIGIBLE FOR NEXT AP STEP (PAYMENT HAS NOT OCCURRED)"
        )

        with self.lock:
            self.evaluated_invoices[invoice_id] = res.model_dump()

        logger.info(
            f"Evaluated invoice {invoice.invoice_number} ({invoice.vendor_name}): "
            f"Decision={decision}, Risk={risk_level}, Discrepancies={len(discrepancies)}"
        )

        return res

    # ------------------------------------------------------------------------
    # Reviewer Override & Audit Lifecycle
    # ------------------------------------------------------------------------
    def apply_reviewer_action(self, invoice_id: str, action_req: ReviewerActionRequest) -> Dict[str, Any]:
        """
        Applies a reviewer action (APPROVE, REJECT, REQUEST_INFO, OVERRIDE_MATCH).
        Preserves original system_decision as immutable.
        Records reviewer ID, timestamp, action, and rationale in audit trail.
        """
        with self.lock:
            inv = self.evaluated_invoices.get(invoice_id)
            if not inv:
                raise KeyError(f"Invoice evaluation ID '{invoice_id}' not found.")

            prev_status = inv.get("final_status", inv.get("system_decision"))
            act_str = action_req.action.strip().upper()

            # Map reviewer action to new final_status
            if act_str == "APPROVE" or act_str == "OVERRIDE_MATCH":
                new_status = "REVIEWER_APPROVED"
            elif act_str == "REJECT":
                new_status = "REVIEWER_REJECTED"
            elif act_str == "REQUEST_INFO":
                new_status = "INFO_REQUESTED"
            else:
                new_status = f"REVIEWER_{act_str}"

            now_iso = datetime.now(timezone.utc).isoformat()
            audit_entry = {
                "action_id": f"AUD-{uuid.uuid4().hex[:6].upper()}",
                "timestamp": now_iso,
                "reviewer_id": action_req.reviewer_id.strip(),
                "action": act_str,
                "previous_status": prev_status,
                "new_status": new_status,
                "rationale": action_req.rationale.strip(),
                "metadata": {
                    "original_system_decision": inv.get("system_decision"),
                    "notes": action_req.notes
                }
            }

            inv["final_status"] = new_status
            if "audit_trail" not in inv:
                inv["audit_trail"] = []
            inv["audit_trail"].append(audit_entry)

            logger.info(
                f"Reviewer action applied: invoice_id={invoice_id}, reviewer={action_req.reviewer_id}, "
                f"action={act_str}, prev={prev_status} -> new={new_status}"
            )
            return inv

    def get_invoice(self, invoice_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self.evaluated_invoices.get(invoice_id)

    def list_invoices(self) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.evaluated_invoices.values())

    # ------------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------------
    def get_metrics(self) -> Dict[str, Any]:
        with self.lock:
            invoices = list(self.evaluated_invoices.values())

        total = len(invoices)
        if total == 0:
            return {
                "total_invoices": 0,
                "auto_approved_count": 0,
                "flagged_for_review_count": 0,
                "rejected_count": 0,
                "reviewer_overrides_count": 0,
                "auto_approve_rate_pct": 0.0,
                "flagged_rate_pct": 0.0,
                "rejection_rate_pct": 0.0,
                "total_spend_evaluated": 0.0,
                "average_confidence": 0.0,
                "disclaimer": "FlowPilot is a decision-support policy engine. AUTO_APPROVE denotes eligibility for next AP step; no payments are executed."
            }

        auto_app = sum(1 for i in invoices if i.get("system_decision") == DecisionEnum.AUTO_APPROVE.value)
        flagged = sum(1 for i in invoices if i.get("system_decision") == DecisionEnum.FLAG_FOR_REVIEW.value)
        rejected = sum(1 for i in invoices if i.get("system_decision") == DecisionEnum.REJECT.value)

        # Overrides: instances where final_status != system_decision
        overrides = sum(1 for i in invoices if i.get("final_status") != i.get("system_decision"))

        total_spend = sum(float(i.get("total_amount", 0.0)) for i in invoices)
        avg_conf = sum(float(i.get("confidence_score", 1.0)) for i in invoices) / total

        return {
            "total_invoices": total,
            "auto_approved_count": auto_app,
            "flagged_for_review_count": flagged,
            "rejected_count": rejected,
            "reviewer_overrides_count": overrides,
            "auto_approve_rate_pct": round((auto_app / total) * 100, 1),
            "flagged_rate_pct": round((flagged / total) * 100, 1),
            "rejection_rate_pct": round((rejected / total) * 100, 1),
            "total_spend_evaluated": round(total_spend, 2),
            "average_confidence": round(avg_conf, 2),
            "disclaimer": "FlowPilot is a decision-support policy engine. AUTO_APPROVE denotes eligibility for next AP step; no payments are executed."
        }

    # ------------------------------------------------------------------------
    # Export Helpers (CSV & JSON)
    # ------------------------------------------------------------------------
    def export_csv(self) -> str:
        """Generates RFC 4180 compliant CSV export of AP evaluation records."""
        output = io.StringIO()
        fieldnames = [
            "invoice_id", "invoice_number", "vendor_name", "po_number", "currency",
            "total_amount", "system_decision", "final_status", "risk_level",
            "confidence_score", "discrepancies_count", "discrepancies_summary",
            "reviewer_override", "last_reviewer", "evaluated_at", "payment_disclaimer"
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        with self.lock:
            invoices = list(self.evaluated_invoices.values())

        for inv in invoices:
            audit = inv.get("audit_trail", [])
            last_review = audit[-1] if len(audit) > 1 else None
            writer.writerow({
                "invoice_id": inv.get("invoice_id"),
                "invoice_number": inv.get("invoice_number"),
                "vendor_name": inv.get("vendor_name"),
                "po_number": inv.get("po_number") or "N/A",
                "currency": inv.get("currency", "USD"),
                "total_amount": inv.get("total_amount"),
                "system_decision": inv.get("system_decision"),
                "final_status": inv.get("final_status"),
                "risk_level": inv.get("risk_level"),
                "confidence_score": inv.get("confidence_score"),
                "discrepancies_count": len(inv.get("discrepancies", [])),
                "discrepancies_summary": " | ".join(inv.get("discrepancies", [])),
                "reviewer_override": "YES" if inv.get("final_status") != inv.get("system_decision") else "NO",
                "last_reviewer": last_review.get("reviewer_id") if last_review else "SYSTEM",
                "evaluated_at": inv.get("evaluated_at"),
                "payment_disclaimer": "ELIGIBLE FOR NEXT AP STEP (PAYMENT HAS NOT OCCURRED)"
            })

        return output.getvalue()

    def export_json(self) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.evaluated_invoices.values())
