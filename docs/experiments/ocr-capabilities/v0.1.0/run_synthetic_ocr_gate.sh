#!/bin/sh
# Generates only synthetic, non-sensitive inputs beneath a new temporary directory.
# It emits aggregate gate status and never prints recognized text.
set -eu

TASK_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/archeos-ocr-gate.XXXXXX")"
trap 'rm -rf "$TASK_TMP_DIR"' EXIT HUP INT TERM
TASK_IMAGE="$TASK_TMP_DIR/mixed.png"
TASK_LOW_CONTRAST="$TASK_TMP_DIR/low-contrast.png"
TASK_NO_TEXT="$TASK_TMP_DIR/no-text.png"
TASK_ROTATED="$TASK_TMP_DIR/rotated.png"
TASK_SCAN_PDF="$TASK_TMP_DIR/mixed-scan.pdf"
TASK_EMPTY_TESSDATA="$TASK_TMP_DIR/empty-tessdata"
mkdir "$TASK_EMPTY_TESSDATA"

generate_image() {
  swift -e 'import AppKit; let output=URL(fileURLWithPath:CommandLine.arguments[1]); let mode=CommandLine.arguments[2]; let size=NSSize(width:1200,height:800); let image=NSImage(size:size); image.lockFocus(); NSColor.white.setFill(); NSBezierPath(rect:NSRect(origin:.zero,size:size)).fill(); if mode != "none" { let color: NSColor = mode == "low" ? NSColor(calibratedWhite:0.72,alpha:1) : NSColor.black; let attributes:[NSAttributedString.Key:Any]=[.font:NSFont.systemFont(ofSize:48),.foregroundColor:color]; ("SYNTHETIC OCR 123\\n中文 测试 456\\nA  B  C\\n1  2  3").draw(at:NSPoint(x:80,y:480),withAttributes:attributes) }; image.unlockFocus(); let bitmap=NSBitmapImageRep(data:image.tiffRepresentation!)!; try bitmap.representation(using:.png,properties:[:])!.write(to:output)' "$1" "$2"
}
generate_image "$TASK_IMAGE" normal
generate_image "$TASK_LOW_CONTRAST" low
generate_image "$TASK_NO_TEXT" none
sips -r 90 "$TASK_IMAGE" --out "$TASK_ROTATED" > /dev/null
sips -s format pdf "$TASK_IMAGE" --out "$TASK_SCAN_PDF" > /dev/null
touch "$TASK_TMP_DIR/invalid-input.bin"

TASK_TESSERACT="${TESSERACT_BIN:-tesseract}"
: "${TESSDATA_DIR:?Set TESSDATA_DIR to the pre-audited local traineddata directory.}"
TASK_TESSDATA="$TESSDATA_DIR"
run_case() {
  TASK_CASE_NAME="$1"
  TASK_CASE_INPUT="$2"
  TASK_CASE_OUTPUT="$TASK_TMP_DIR/$TASK_CASE_NAME.tsv"
  set +e
  "$TASK_TESSERACT" "$TASK_CASE_INPUT" "$TASK_TMP_DIR/$TASK_CASE_NAME" --tessdata-dir "$TASK_TESSDATA" -l eng+chi_sim tsv > /dev/null 2> "$TASK_TMP_DIR/$TASK_CASE_NAME.err"
  TASK_CASE_EXIT=$?
  set -e
  if [ "$TASK_CASE_EXIT" -eq 0 ] && head -n 1 "$TASK_CASE_OUTPUT" | grep -q 'left.*top.*width.*height.*conf'; then
    TASK_CASE_FIELDS=present
    TASK_CASE_WORDS="$(awk -F '\t' '$1 == 5 { count++ } END { print count + 0 }' "$TASK_CASE_OUTPUT")"
  else
    TASK_CASE_FIELDS=unavailable
    TASK_CASE_WORDS=0
  fi
  printf '%s\n' "case=$TASK_CASE_NAME exit=$TASK_CASE_EXIT tsv_bbox_confidence=$TASK_CASE_FIELDS word_rows=$TASK_CASE_WORDS"
}

run_case mixed_print "$TASK_IMAGE"
run_case rotated_print "$TASK_ROTATED"
run_case low_contrast_print "$TASK_LOW_CONTRAST"
run_case no_text "$TASK_NO_TEXT"
run_case invalid_input "$TASK_TMP_DIR/invalid-input.bin"
run_case scan_pdf_direct "$TASK_SCAN_PDF"

set +e
"$TASK_TESSERACT" "$TASK_IMAGE" "$TASK_TMP_DIR/missing-model" --tessdata-dir "$TASK_EMPTY_TESSDATA" -l eng tsv > /dev/null 2>&1
TASK_MISSING_MODEL_EXIT=$?
set -e

printf '%s\n' "engine=tesseract" "missing_model_exit=$TASK_MISSING_MODEL_EXIT" "temporary_directory_cleanup=scheduled"
