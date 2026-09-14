#!/usr/bin/env bash
# soffice must not run VBA / AutoOpen or fetch a macro payload.
# MacroSecurityLevel 3 + disposable profile; wrap must not pass --safe-mode.
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
  <Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>
  <Override PartName="/word/vbaData.xml" ContentType="application/vnd.ms-word.vbaData+xml"/>
</Types>
"""
rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
doc = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>golden-docm</w:t></w:r></w:p></w:body>
</w:document>
"""
doc_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdVba" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" Target="vbaProject.bin"/>
  <Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" Target="http://127.0.0.1:9/autoopen.bin" TargetMode="External"/>
</Relationships>
"""
vba_data = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<wne:vbaSuppData xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml">
  <wne:docEvents>
    <wne:eventDocOpen/>
  </wne:docEvents>
</wne:vbaSuppData>
"""
# Placeholder CFB-shaped blob. Real VBA would Auto_Open-fetch this URL.
vba_bin = b"Auto_Open\nhttp://127.0.0.1:9/autoopen.bin\nSub Document_Open()\nEnd Sub\n"
with ZipFile("macro.docm", "w", ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml", ct)
    z.writestr("_rels/.rels", rels)
    z.writestr("word/document.xml", doc)
    z.writestr("word/_rels/document.xml.rels", doc_rels)
    z.writestr("word/vbaData.xml", vba_data)
    z.writestr("word/vbaProject.bin", vba_bin)
PY

if [ ! -f /etc/sera-toolbox/libreoffice/registrymodifications.xcu ]; then
  echo "FAIL missing LibreOffice MacroSecurityLevel xcu" >&2
  exit 1
fi
if ! grep -q 'MacroSecurityLevel' /etc/sera-toolbox/libreoffice/registrymodifications.xcu \
  || ! grep -q '<value>3</value>' /etc/sera-toolbox/libreoffice/registrymodifications.xcu; then
  echo "FAIL MacroSecurityLevel is not 3" >&2
  exit 1
fi

set +e
timeout 60s soffice --headless --nologo --nolockcheck --norestore \
  --convert-to pdf macro.docm >out 2>err
status=$?
set -e
if grep -qi '127.0.0.1' err out 2>/dev/null; then
  echo "FAIL soffice logged macro/external fetch" >&2
  exit 1
fi
if grep -qi 'autoopen' err out 2>/dev/null; then
  echo "FAIL soffice logged AutoOpen payload" >&2
  exit 1
fi
echo "OK soffice VBA/AutoOpen did not run (exit=$status)"
