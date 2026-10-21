; Names that identify a Saylight install, shared by saylight.iss, make_test_copy.py and lifecycle_test.ps1.
;
; "ISCC /DTEST saylight.iss" builds an installer with the TEST names, and make_test_copy.py gives the app
; the same TEST names, so a test build has its own AppId, folder, quit event, Run value, shortcut and
; data folder and can never meet a real Saylight install.
;
; Keep one #define per line in the form  #define Name "Value"  (the Python and PowerShell readers rely on it).

#ifdef TEST
  #define AppName        "Saylight Test"
  #define AppIdGuid      "{{3CFA584D-23F9-4643-B33E-37AD4B0F87AC}"
  #define RunValue       "SaylightTest"
  #define QuitEvent      "Local\SaylightTest.Quit"
  #define AumId          "Saylight.Test"
  #define ShortcutName   "Saylight Test"
  #define DataDirName    "SaylightTest"
  #define CachePrefix    "saylighttest_"
  #define OpenPipe       "SaylightTest.Open"
  #define ProgId         "SaylightTest.Document"
  #define OldRunValue    "SaylightTest.Old"
  #define OldQuitEvent   "Local\SaylightTest.Old.Quit"
  #define OldShortcut    "Saylight Test Old"
  #define OldCachePrefix "saylighttest_old_"
#else
  #define AppName        "Saylight"
  ; Never change the AppId: upgrades and Settings > Apps find the install by it.
  #define AppIdGuid      "{{CCD9FFFC-0E70-4141-A902-797526A7237D}"
  ; The sign-in entry 1.0's --install wrote, so an upgrade replaces it.
  #define RunValue       "Saylight"
  ; Keeps the app to one copy; setting it asks the running copy to quit.
  #define QuitEvent      "Local\Saylight.Quit"
  #define AumId          "Saylight.App"
  #define ShortcutName   "Saylight"
  ; %LOCALAPPDATA%\Saylight holds settings, logs and later data files.
  #define DataDirName    "Saylight"
  #define CachePrefix    "saylight_"
  #define OpenPipe       "Saylight.Open"
  ; Lists Saylight under "Open with" for the file kinds it reads; it is never made the default app.
  #define ProgId         "Saylight.Document"
  ; The app's earlier name ("Read Aloud").
  #define OldRunValue    "ReadAloudHotkey"
  #define OldQuitEvent   "Local\ReadAloudHotkey.Quit"
  #define OldShortcut    "Read Aloud"
  #define OldCachePrefix "read_aloud_"
#endif
