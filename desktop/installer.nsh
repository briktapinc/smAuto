; Bubble Pod NSIS hooks — never carry API keys / ngrok / YouTube OAuth across installs.
; Projects under user_data/projects are left intact.

!macro customInstall
  Delete "$APPDATA\Bubble Pod\user_data\settings.json"
  Delete "$APPDATA\Bubble Pod\user_data\youtube_token.json"
  Delete "$APPDATA\Bubble Pod\user_data\auth.json"
  Delete "$APPDATA\Bubble Pod\user_data\members.json"
  Delete "$APPDATA\Bubble Pod\user_data\ngrok.pid"
  Delete "$APPDATA\Bubble Pod\user_data\ngrok_traffic_policy.json"
  Delete "$APPDATA\Bubble Pod\user_data\listen_port.json"
  Delete "$APPDATA\Bubble Pod\user_data\.env"
  Delete "$APPDATA\Bubble Pod\user_data\.env.local"
!macroend
