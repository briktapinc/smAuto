; Stickman Automation NSIS hooks — never carry API keys / ngrok / YouTube OAuth across installs.
; Projects under user_data/projects are left intact.

!macro customInstall
  Delete "$APPDATA\Stickman Automation\user_data\settings.json"
  Delete "$APPDATA\Stickman Automation\user_data\youtube_token.json"
  Delete "$APPDATA\Stickman Automation\user_data\auth.json"
  Delete "$APPDATA\Stickman Automation\user_data\members.json"
  Delete "$APPDATA\Stickman Automation\user_data\ngrok.pid"
  Delete "$APPDATA\Stickman Automation\user_data\ngrok_traffic_policy.json"
  Delete "$APPDATA\Stickman Automation\user_data\listen_port.json"
  Delete "$APPDATA\Stickman Automation\user_data\.env"
  Delete "$APPDATA\Stickman Automation\user_data\.env.local"
!macroend
