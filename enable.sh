chmod +x bot_run.sh disable.sh
systemctl daemon-reload
systemctl enable bridgeit
systemctl start bridgeit