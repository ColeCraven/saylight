# Write word_made.docx and word_made.pdf with Microsoft Word itself (run again only to change them).
#   powershell -NoProfile -ExecutionPolicy Bypass -File make_word_fixtures.ps1
# Word runs hidden and is closed afterwards. It refuses to run while Word is already open, so it never touches
# a document someone is working on. The text is made up for these tests.

$ErrorActionPreference = "Stop"
if (Get-Process WINWORD -ErrorAction SilentlyContinue) { throw "Close Word first." }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$word = New-Object -ComObject Word.Application
try {
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $doc = $word.Documents.Add()
    $sel = $word.Selection
    function Para([string]$text, [string]$style = "Normal") {
        $sel.Style = $doc.Styles.Item($style)
        $sel.TypeText($text)
        $sel.TypeParagraph()
    }
    Para "Listening While Reading" "Title"
    Para "Introduction" "Heading 1"
    $sel.Style = $doc.Styles.Item("Normal")
    $sel.TypeText("Many students with dyslexia listen to a text while they read it (Wood et al., 2018). ")
    $sel.TypeText("Reading along may help them keep their place")
    $doc.Footnotes.Add($sel.Range, [Type]::Missing, "A footnote written in Word.") | Out-Null
    $sel.TypeText(" and understand more of what they read. This sentence is long enough to wrap onto a second " +
                  "line in the PDF, so that the reader has to join the lines again.")
    $sel.TypeParagraph()
    Para "Method" "Heading 1"
    Para "Participants read twelve passages, e.g. from first-year textbooks."
    Para "Recruit the participants" "List Number"
    Para "Run the reading task" "List Number"
    $sel.Style = $doc.Styles.Item("Normal")
    $table = $doc.Tables.Add($sel.Range, 3, 3)
    $cells = @(@("Group", "Accuracy", "Time"), @("Dyslexic", "0.81", "1.2"), @("Control", "0.93", "0.9"))
    for ($r = 0; $r -lt 3; $r++) { for ($c = 0; $c -lt 3; $c++) {
        $table.Cell($r + 1, $c + 1).Range.Text = $cells[$r][$c] } }
    $sel.EndKey(6) | Out-Null
    Para "The voice helped both groups."
    Para "References" "Heading 1"
    Para "Wood, S. G., Moxley, J. H., Tighe, E. L., and Wagner, R. K. (2018). Does use of text-to-speech and related read-aloud tools improve reading comprehension? Journal of Learning Disabilities, 51(1), 73-84."
    $docx = Join-Path $here "word_made.docx"
    $pdf = Join-Path $here "word_made.pdf"
    $doc.SaveAs2([string]$docx, 16, $false, "", $false)   # .docx, not added to Word's recent files
    $doc.ExportAsFixedFormat([string]$pdf, 17)               # PDF
    $doc.Close(0)
} finally {
    $word.Quit([ref]0)
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
}

# Word puts the user's name in the .docx properties (the PDF has none): replace it before the file is shared.
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open((Join-Path $here "word_made.docx"), "Update")
try {
    $entry = $zip.GetEntry("docProps/core.xml")
    $reader = New-Object System.IO.StreamReader($entry.Open())
    $xml = $reader.ReadToEnd()
    $reader.Close()
    $xml = $xml -replace "<dc:creator>[^<]*</dc:creator>", "<dc:creator>Saylight tests</dc:creator>"
    $xml = $xml -replace "<cp:lastModifiedBy>[^<]*</cp:lastModifiedBy>", "<cp:lastModifiedBy>Saylight tests</cp:lastModifiedBy>"
    $entry.Delete()
    $writer = New-Object System.IO.StreamWriter($zip.CreateEntry("docProps/core.xml").Open())
    $writer.Write($xml)
    $writer.Close()
} finally {
    $zip.Dispose()
}
"written: word_made.docx, word_made.pdf"
