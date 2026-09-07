#!/usr/bin/env bash
# soffice must not follow an external http relationship.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
python3 - <<'PY'
from zipfile import ZipFile, ZIP_DEFLATED
ct = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
doc = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>golden-docx</w:t></w:r></w:p></w:body>
</w:document>
"""
ext = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" Target="http://127.0.0.1:9/macro.bin" TargetMode="External"/>
</Relationships>
"""
with ZipFile("ext.docx", "w", ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml", ct)
    z.writestr("_rels/.rels", rels)
    z.writestr("word/document.xml", doc)
    z.writestr("word/_rels/document.xml.rels", ext)
PY

set +e
timeout 60s soffice --headless --nologo --nolockcheck --norestore \
  --convert-to pdf ext.docx >out 2>err
status=$?
set -e
if grep -qi '127.0.0.1' err out 2>/dev/null; then
  echo "FAIL soffice logged external fetch" >&2
  exit 1
fi
echo "OK soffice external rel did not fetch (exit=$status)"
