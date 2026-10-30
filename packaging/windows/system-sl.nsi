; NSIS Installer Script for system-sl
; Build with: makensis system-sl.nsi
; Requires NSIS 3.x

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

;--------------------------------
; General Settings
;--------------------------------
Name "THE SYSTEM"
OutFile "system-sl-setup.exe"
InstallDir "$LOCALAPPDATA\system-sl"
InstallDirRegKey HKCU "Software\system-sl" ""
RequestExecutionLevel user
Unicode true

;--------------------------------
; Version
;--------------------------------
VIProductVersion "1.2.0.0"
VIAddVersionKey "ProductName" "THE SYSTEM"
VIAddVersionKey "CompanyName" "sproutcake23"
VIAddVersionKey "LegalCopyright" "GPL-3.0-or-later"
VIAddVersionKey "FileDescription" "Solo Leveling-inspired task management system"
VIAddVersionKey "FileVersion" "1.2.0"

;--------------------------------
; MUI Settings
;--------------------------------
!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"

!define MUI_WELCOMEPAGE_TITLE "Welcome to THE SYSTEM Setup"
!define MUI_WELCOMEPAGE_TEXT "This wizard will install THE SYSTEM - a Solo Leveling-inspired task management system on your computer."
!define MUI_FINISHPAGE_TITLE "Setup Complete"
!define MUI_FINISHPAGE_TEXT "THE SYSTEM has been installed. Click Finish to launch the application."

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

;--------------------------------
; Sections
;--------------------------------
Section "Main Program" SecMain
    SetOutPath "$INSTDIR"

    ; Main executable
    File "system-sl.exe"

    ; Assets (sounds)
    SetOutPath "$INSTDIR\sounds"
    File /r "sounds\*.mp3"
    File /r "sounds\*.wav"

    ; Create uninstaller
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Registry for uninstaller
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\system-sl" \
        "DisplayName" "THE SYSTEM"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\system-sl" \
        "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\system-sl" \
        "DisplayVersion" "1.2.0"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\system-sl" \
        "Publisher" "sproutcake23"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\system-sl" \
        "URLInfoAbout" "https://github.com/sproutcake23/System-SL"

    ; Store install path for updates
    WriteRegStr HKCU "Software\system-sl" "" "$INSTDIR"
SectionEnd

Section "Start Menu Shortcuts" SecStartMenu
    CreateDirectory "$SMPROGRAMS\THE SYSTEM"
    CreateShortcut "$SMPROGRAMS\THE SYSTEM\THE SYSTEM.lnk" "$INSTDIR\system-sl.exe" "" "$INSTDIR\system-sl.exe" 0
    CreateShortcut "$SMPROGRAMS\THE SYSTEM\Uninstall.lnk" "$INSTDIR\uninstall.exe"
SectionEnd

Section "Desktop Shortcut" SecDesktop
    CreateShortcut "$DESKTOP\THE SYSTEM.lnk" "$INSTDIR\system-sl.exe" "" "$INSTDIR\system-sl.exe" 0
SectionEnd

;--------------------------------
; Uninstaller
;--------------------------------
Section "Uninstall"
    ; Remove files
    Delete "$INSTDIR\system-sl.exe"
    Delete "$INSTDIR\system-sl.exe.old"
    Delete "$INSTDIR\system-sl.exe.new"
    Delete "$INSTDIR\uninstall.exe"

    ; Remove sounds directory
    RMDir /r "$INSTDIR\sounds"

    ; Remove main directory
    RMDir "$INSTDIR"

    ; Remove shortcuts
    Delete "$SMPROGRAMS\THE SYSTEM\THE SYSTEM.lnk"
    Delete "$SMPROGRAMS\THE SYSTEM\Uninstall.lnk"
    RMDir "$SMPROGRAMS\THE SYSTEM"
    Delete "$DESKTOP\THE SYSTEM.lnk"

    ; Remove the autostart entry, or Windows keeps launching a deleted exe
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "SL-Notifier"

    ; Remove registry
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\system-sl"
    DeleteRegKey HKCU "Software\system-sl"
SectionEnd

;--------------------------------
; Functions
;--------------------------------
Function .onInit
    ; Check if already installed
    ReadRegStr $0 HKCU "Software\system-sl" ""
    ${If} $0 != ""
        MessageBox MB_YESNO|MB_ICONQUESTION \
            "THE SYSTEM is already installed. Do you want to reinstall?" \
            IDYES +2
        Abort
    ${EndIf}
FunctionEnd

Function un.onUninstallSuccess
    HideWindow
    MessageBox MB_OK|MB_ICONINFORMATION "THE SYSTEM has been successfully uninstalled."
FunctionEnd