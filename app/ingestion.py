import re
import io
import csv
import json
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import docx
except ImportError:
    docx = None


MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_RECORDS_LIMIT = 500

SUPPORTED_EXTENSIONS = {
    '.csv', '.tsv', '.json', '.xlsx', '.xls',
    '.txt', '.md', '.log',
    '.pdf', '.docx', '.doc',
    '.html', '.xml'
}

MESSAGE_FIELD_CANDIDATES = [
    'message', 'request', 'text', 'query', 'issue', 'ticket', 'description',
    'notes', 'complaint', 'customer_message', 'customer_query', 'ticket_description',
    'request_details', 'issue_summary', 'details', 'body', 'content', 'summary', 'customer_request'
]

ID_FIELD_CANDIDATES = [
    'id', 'record_id', 'case_id', 'ticket_id', 'order_id', 'customer_id',
    'ticket_number', 'case_reference', 'ref', 'reference', 'row_id'
]

AMOUNT_FIELD_CANDIDATES = [
    'amount', 'refund_value', 'value', 'price', 'cost', 'refund_amount',
    'transaction_value', 'total', 'charge', 'dispute_amount'
]

CATEGORY_FIELD_CANDIDATES = [
    'category', 'type', 'intent', 'topic', 'department', 'issue_type', 'subject'
]

RISK_FIELD_CANDIDATES = [
    'risk', 'severity', 'priority', 'risk_level'
]


class HTMLTextExtractor(HTMLParser):
    """Safely extracts text and table data from HTML documents without executing scripts."""
    def __init__(self):
        super().__init__()
        self.text_chunks: List[str] = []
        self.in_table = False
        self.in_row = False
        self.current_row: List[str] = []
        self.table_rows: List[List[str]] = []
        self.ignore_tag = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'meta', 'link'):
            self.ignore_tag = True
        elif tag == 'table':
            self.in_table = True
        elif tag == 'tr':
            self.in_row = True
            self.current_row = []

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'meta', 'link'):
            self.ignore_tag = False
        elif tag == 'table':
            self.in_table = False
        elif tag == 'tr':
            self.in_row = False
            if self.current_row and any(c.strip() for c in self.current_row):
                self.table_rows.append(self.current_row)

    def handle_data(self, data):
        if not self.ignore_tag:
            cleaned = data.strip()
            if cleaned:
                if self.in_row:
                    self.current_row.append(cleaned)
                else:
                    self.text_chunks.append(cleaned)


def extract_order_reference(text: str) -> Optional[str]:
    """Detects order or invoice references like ORD-1234, INV-9901, TICKET-44."""
    m = re.search(r'\b(ORD-[\w\d]+|INV-[\w\d]+|TICKET-[\w\d]+|CASE-[\w\d]+|REF-[\w\d]+|#\d{4,8})\b', text, re.IGNORECASE)
    return m.group(1) if m else None


def extract_currency(text: str) -> str:
    """Identifies currency symbols or ISO codes."""
    m = text.lower()
    if '₹' in m or 'rs' in m or 'inr' in m:
        return 'INR'
    if '$' in m or 'usd' in m:
        return 'USD'
    if '€' in m or 'eur' in m:
        return 'EUR'
    if '£' in m or 'gbp' in m:
        return 'GBP'
    return 'INR'


def sanitize_filename(filename: str) -> str:
    """Removes path traversal and unsafe characters."""
    name = Path(filename).name
    return re.sub(r'[^\w\.\-\s]', '_', name)


def detect_file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    mapping = {
        '.csv': 'CSV Spreadsheet',
        '.tsv': 'TSV Tab-Separated Values',
        '.json': 'JSON Document',
        '.xlsx': 'Excel Workbook (XLSX)',
        '.xls': 'Legacy Excel Workbook (XLS)',
        '.txt': 'Plain Text File',
        '.md': 'Markdown Document',
        '.log': 'Application Log File',
        '.pdf': 'PDF Document',
        '.docx': 'Word Document (DOCX)',
        '.doc': 'Legacy Word Document (DOC)',
        '.html': 'HTML Web Document',
        '.xml': 'XML Data Document'
    }
    return mapping.get(ext, 'Unknown Document')


def parse_unstructured_text(text: str, source_file: str, source_type: str) -> List[Dict[str, Any]]:
    """
    Intelligently segments unstructured text (from TXT, MD, LOG, PDF, DOCX, HTML)
    into distinct customer ticket records.
    """
    cleaned = text.strip()
    if not cleaned:
        return []

    # Strategy 1: Check for explicit ticket / case / customer headers
    ticket_blocks = re.split(r'\n+(?=(?:case\s*#?|ticket\s*#?|request\s*#?|customer\s*#?|email\s*#?|\[\d{4}-\d{2}-\d{2})\b)', cleaned, flags=re.IGNORECASE)
    if len(ticket_blocks) > 1 and all(b.strip() for b in ticket_blocks):
        blocks = [b.strip() for b in ticket_blocks if b.strip()]
    else:
        # Strategy 2: Check for double newline separated records
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n+', cleaned) if p.strip()]
        if len(paragraphs) > 1:
            blocks = paragraphs
        else:
            # Strategy 3: Check for numbered list records (1. Request..., 2. Request...)
            numbered = re.split(r'\n+(?=\d+[\.\)]\s+)', cleaned)
            if len(numbered) > 1:
                blocks = [n.strip() for n in numbered if n.strip()]
            else:
                blocks = [cleaned]

    records: List[Dict[str, Any]] = []
    for idx, block in enumerate(blocks[:MAX_RECORDS_LIMIT]):
        rec_id = f"EXT-{idx+1:03d}"
        detected_id = extract_order_reference(block)
        records.append({
            'record_id': detected_id or rec_id,
            'source_file': source_file,
            'source_type': source_type,
            'message': block,
            'raw_text': block
        })

    return records


def ingest_file(filename: str, raw_bytes: bytes) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Universal ingestion processor.
    Validates, extracts, and standardizes records from any supported format.
    Returns: (raw_records, metadata)
    """
    clean_name = sanitize_filename(filename)
    ext = Path(clean_name).suffix.lower()

    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported format '{ext}'. Allowed file types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")

    if len(raw_bytes) == 0:
        raise ValueError("Uploaded file is empty (0 bytes).")

    if len(raw_bytes) > MAX_FILE_SIZE:
        raise ValueError(f"File exceeds maximum upload limit of 5 MB ({len(raw_bytes) / 1024 / 1024:.2f} MB).")

    records: List[Dict[str, Any]] = []
    metadata: Dict[str, Any] = {
        'filename': clean_name,
        'extension': ext,
        'file_type': detect_file_type(clean_name),
        'file_size_bytes': len(raw_bytes),
        'warnings': [],
        'is_tabular': False
    }

    # 1. CSV & TSV
    if ext in ('.csv', '.tsv'):
        delimiter = '\t' if ext == '.tsv' else ','
        try:
            content = raw_bytes.decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
            for idx, r in enumerate(reader):
                if any(v.strip() for v in r.values() if v):
                    d = dict(r)
                    d['_source_file'] = clean_name
                    d['_source_type'] = ext[1:]
                    records.append(d)
            metadata['is_tabular'] = True
        except Exception as e:
            raise ValueError(f"Malformed {ext.upper()} file: {str(e)}")

    # 2. JSON
    elif ext == '.json':
        try:
            content = raw_bytes.decode('utf-8')
            parsed = json.loads(content)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        d = dict(item)
                        d['_source_file'] = clean_name
                        d['_source_type'] = 'json'
                        records.append(d)
                metadata['is_tabular'] = True
            elif isinstance(parsed, dict):
                # Check for array envelope: {"cases": [...]}, {"tickets": [...]}, {"data": [...]}
                candidates = ['cases', 'tickets', 'records', 'items', 'data', 'evaluations']
                arr = next((parsed[k] for k in candidates if k in parsed and isinstance(parsed[k], list)), None)
                if arr:
                    for item in arr:
                        if isinstance(item, dict):
                            d = dict(item)
                            d['_source_file'] = clean_name
                            d['_source_type'] = 'json'
                            records.append(d)
                    metadata['is_tabular'] = True
                else:
                    d = dict(parsed)
                    d['_source_file'] = clean_name
                    d['_source_type'] = 'json'
                    records.append(d)
                    metadata['is_tabular'] = True
            else:
                raise ValueError("JSON file must contain an object or array of objects.")
        except json.JSONDecodeError as e:
            raise ValueError(f"Malformed JSON file: {str(e)}")

    # 3. XLSX
    elif ext == '.xlsx':
        if openpyxl is None:
            raise RuntimeError("openpyxl library is required on the server to parse XLSX.")
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
            sheet = wb.active
            iter_rows = list(sheet.iter_rows(values_only=True))
            if iter_rows and len(iter_rows) > 1:
                headers = [str(h).strip() if h is not None else f'col_{i}' for i, h in enumerate(iter_rows[0])]
                for row_vals in iter_rows[1:]:
                    if any(v is not None for v in row_vals):
                        row_dict = {headers[i]: (row_vals[i] if i < len(row_vals) else None) for i in range(len(headers))}
                        row_dict['_source_file'] = clean_name
                        row_dict['_source_type'] = 'xlsx'
                        records.append(row_dict)
            metadata['is_tabular'] = True
        except Exception as e:
            raise ValueError(f"Malformed XLSX spreadsheet: {str(e)}")

    # 4. XLS (Legacy)
    elif ext == '.xls':
        metadata['warnings'].append("Legacy .xls format detected. Please save as modern .xlsx for guaranteed formatting.")
        raise ValueError("Legacy .xls binary format is not supported directly. Please convert to modern .xlsx or .csv.")

    # 5. Plain Text, Markdown, Log
    elif ext in ('.txt', '.md', '.log'):
        try:
            text = raw_bytes.decode('utf-8', errors='replace')
            records = parse_unstructured_text(text, clean_name, ext[1:])
        except Exception as e:
            raise ValueError(f"Error reading text document: {str(e)}")

    # 6. PDF
    elif ext == '.pdf':
        if pypdf is None:
            raise RuntimeError("pypdf library is required on the server to parse PDF documents.")
        try:
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
            total_pages = len(reader.pages)
            metadata['page_count'] = total_pages
            text_parts = []
            for page in reader.pages:
                extracted = page.extract_text() or ''
                text_parts.append(extracted)
            full_text = "\n\n".join(text_parts).strip()

            # Scanned / Image-only PDF detection
            if total_pages > 0 and len(full_text) < 20:
                metadata['warnings'].append(
                    f"Scanned image PDF detected ({total_pages} page(s) with minimal extractable text). OCR engine is required for non-text PDFs."
                )
                raise ValueError("This PDF appears to be a scanned image with no extractable text layer. An OCR engine is required to extract image-only documents.")

            records = parse_unstructured_text(full_text, clean_name, 'pdf')
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Corrupt or unreadable PDF document: {str(e)}")

    # 7. DOCX
    elif ext == '.docx':
        if docx is None:
            raise RuntimeError("python-docx library is required on the server to parse Word documents.")
        try:
            doc = docx.Document(io.BytesIO(raw_bytes))
            
            # Check if document has tables with data
            if doc.tables:
                for table in doc.tables:
                    if len(table.rows) > 1:
                        headers = [cell.text.strip() or f'col_{ci}' for ci, cell in enumerate(table.rows[0].cells)]
                        for row in table.rows[1:]:
                            vals = [cell.text.strip() for cell in row.cells]
                            if any(vals):
                                row_dict = {headers[ci]: vals[ci] for ci in range(min(len(headers), len(vals)))}
                                row_dict['_source_file'] = clean_name
                                row_dict['_source_type'] = 'docx'
                                records.append(row_dict)
                if records:
                    metadata['is_tabular'] = True

            # If no tables or empty tables, parse document paragraphs
            if not records:
                paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
                full_text = "\n\n".join(paras)
                records = parse_unstructured_text(full_text, clean_name, 'docx')
        except Exception as e:
            raise ValueError(f"Malformed or corrupt Word document (.docx): {str(e)}")

    # 8. DOC (Legacy Word)
    elif ext == '.doc':
        raise ValueError("Legacy binary .doc format is not supported. Please save as modern .docx or export to .pdf/.txt.")

    # 9. HTML
    elif ext == '.html':
        try:
            html_text = raw_bytes.decode('utf-8', errors='replace')
            parser = HTMLTextExtractor()
            parser.feed(html_text)

            # If tabular HTML table exists
            if parser.table_rows and len(parser.table_rows) > 1:
                headers = [h or f'col_{i}' for i, h in enumerate(parser.table_rows[0])]
                for row_vals in parser.table_rows[1:]:
                    row_dict = {headers[i]: (row_vals[i] if i < len(row_vals) else '') for i in range(len(headers))}
                    row_dict['_source_file'] = clean_name
                    row_dict['_source_type'] = 'html'
                    records.append(row_dict)
                metadata['is_tabular'] = True
            else:
                full_text = "\n\n".join(parser.text_chunks)
                records = parse_unstructured_text(full_text, clean_name, 'html')
        except Exception as e:
            raise ValueError(f"Malformed HTML document: {str(e)}")

    # 10. XML
    elif ext == '.xml':
        try:
            # Safe XML parsing: defused from external entity injection
            tree = ET.fromstring(raw_bytes)
            # Look for repeating child nodes
            children = list(tree)
            if children and all(len(c) > 0 or c.attrib for c in children):
                for child in children:
                    row_dict = {elem.tag: (elem.text or '').strip() for elem in child}
                    if child.attrib:
                        row_dict.update({f"@{k}": v for k, v in child.attrib.items()})
                    row_dict['_source_file'] = clean_name
                    row_dict['_source_type'] = 'xml'
                    records.append(row_dict)
                metadata['is_tabular'] = True
            else:
                # Flat XML: extract all element texts
                texts = [elem.text.strip() for elem in tree.iter() if elem.text and elem.text.strip()]
                full_text = "\n\n".join(texts)
                records = parse_unstructured_text(full_text, clean_name, 'xml')
        except ET.ParseError as e:
            raise ValueError(f"Malformed XML file: {str(e)}")

    if not records:
        raise ValueError("No extractable data records or text found in the uploaded file.")

    return records, metadata


def normalize_records(
    raw_records: List[Dict[str, Any]],
    metadata: Dict[str, Any],
    custom_mapping: Optional[Dict[str, str]] = None
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Transforms raw extracted records into FlowPilot Canonical Schema:
      - record_id: str
      - source_file: str
      - source_type: str
      - message: str
      - category: Optional[str]
      - amount: Optional[int]
      - currency: str
      - order_reference: Optional[str]
      - extraction_confidence: 'high' | 'medium' | 'low'
      - validation_status: 'valid' | 'warning' | 'error'
      - warnings: List[str]
    """
    if not raw_records:
        return [], {'status': 'empty', 'columns': [], 'mapping': {}}

    # Inspect available keys
    all_keys = set()
    for r in raw_records:
        all_keys.update(k for k in r.keys() if not k.startswith('_'))
    columns = sorted(list(all_keys))

    # Determine Field Mapping
    mapping = {
        'message_column': None,
        'id_column': None,
        'amount_column': None,
        'category_column': None,
        'risk_column': None
    }

    if custom_mapping:
        for k, v in custom_mapping.items():
            if v and v in columns:
                mapping[k] = v

    def match_candidate(candidates: List[str]) -> Optional[str]:
        # 1. Exact match
        for c in columns:
            normalized = re.sub(r'[\s\-_]+', '_', c.strip().lower())
            if normalized in candidates:
                return c
        # 2. Substring match
        for c in columns:
            normalized = re.sub(r'[\s\-_]+', '_', c.strip().lower())
            if any(cand == normalized or (len(cand) >= 4 and cand in normalized) for cand in candidates):
                return c
        return None

    # Automatic Mapping Candidates
    if not mapping['message_column']:
        mapping['message_column'] = match_candidate(MESSAGE_FIELD_CANDIDATES)

    if not mapping['id_column']:
        mapping['id_column'] = match_candidate(ID_FIELD_CANDIDATES)

    if not mapping['amount_column']:
        mapping['amount_column'] = match_candidate(AMOUNT_FIELD_CANDIDATES)

    if not mapping['category_column']:
        mapping['category_column'] = match_candidate(CATEGORY_FIELD_CANDIDATES)

    if not mapping['risk_column']:
        mapping['risk_column'] = match_candidate(RISK_FIELD_CANDIDATES)

    # If tabular and no message column could be identified
    if metadata.get('is_tabular') and not mapping['message_column']:
        return [], {
            'status': 'mapping_needed',
            'columns': columns,
            'mapping': mapping,
            'message': 'No customer message/request column identified. Please select which column contains customer requests.'
        }

    # Normalize each record
    canonical_list: List[Dict[str, Any]] = []
    seen_ids = set()

    for idx, row in enumerate(raw_records):
        warnings = []
        confidence = 'high' if metadata.get('is_tabular') else 'medium'

        # Extract message text
        msg_col = mapping['message_column']
        if msg_col and row.get(msg_col) is not None:
            raw_msg = str(row[msg_col]).strip()
        elif 'message' in row:
            raw_msg = str(row['message']).strip()
        elif 'raw_text' in row:
            raw_msg = str(row['raw_text']).strip()
        else:
            # Fallback: join non-meta values
            raw_msg = " ".join(str(v) for k, v in row.items() if not k.startswith('_') and v).strip()

        if not raw_msg:
            # Skip purely blank records
            continue

        # Extract ID
        id_col = mapping['id_column']
        if id_col and row.get(id_col):
            record_id = str(row[id_col]).strip()
        elif row.get('record_id'):
            record_id = str(row['record_id']).strip()
        else:
            record_id = f"REC-{idx+1:03d}"

        if record_id in seen_ids:
            warnings.append(f"Duplicate identifier '{record_id}' detected.")
            confidence = 'medium'
        seen_ids.add(record_id)

        # Extract Amount
        amount_col = mapping['amount_column']
        parsed_amount = None
        if amount_col and row.get(amount_col) is not None:
            val_str = str(row[amount_col]).replace(',', '').strip()
            # Try float conversion
            m_num = re.search(r'[\d]+(?:\.\d+)?', val_str)
            if m_num:
                try:
                    parsed_amount = int(float(m_num.group(0)))
                except ValueError:
                    pass

        # If not in amount column, extract from message text
        if parsed_amount is None:
            # Currency patterns in text
            hits = re.findall(r'(?:₹|rs\.?|inr|\$|usd|eur|€)\s*([\d,]+(?:\.\d{1,2})?)', raw_msg.lower())
            if hits:
                try:
                    parsed_amount = int(float(hits[0].replace(',', '')))
                except ValueError:
                    pass
            else:
                # Standalone numbers
                standalone = re.findall(r'\b(\d[\d,]{2,5})\b', raw_msg.lower())
                if standalone:
                    try:
                        parsed_amount = int(float(standalone[0].replace(',', '')))
                    except ValueError:
                        pass

        # Extract category / risk hints
        category_val = None
        if mapping['category_column'] and row.get(mapping['category_column']):
            category_val = str(row[mapping['category_column']]).strip()

        risk_val = None
        if mapping['risk_column'] and row.get(mapping['risk_column']):
            risk_val = str(row[mapping['risk_column']]).strip().upper()

        order_ref = extract_order_reference(raw_msg)
        currency_val = extract_currency(raw_msg)

        # Validation status
        if len(raw_msg) < 5:
            validation_status = 'warning'
            warnings.append("Request text is very short; may lack necessary context.")
            confidence = 'low'
        else:
            validation_status = 'valid'

        canonical_list.append({
            'record_id': record_id,
            'source_file': metadata['filename'],
            'source_type': metadata['extension'][1:],
            'message': raw_msg,
            'category': category_val,
            'amount': parsed_amount,
            'currency': currency_val,
            'order_reference': order_ref,
            'risk': risk_val,
            'extraction_confidence': confidence,
            'validation_status': validation_status,
            'warnings': warnings,
            'source_snippet': raw_msg[:120] + ('…' if len(raw_msg) > 120 else '')
        })

    return canonical_list, {
        'status': 'extracted',
        'columns': columns,
        'mapping': mapping,
        'total_extracted': len(canonical_list)
    }
