#!/bin/bash
set -e

echo "[bridgeit] Git pull"
git pull

echo "[bridgeit] Activating virtual environment"
source venv/bin/activate

echo "[bridgeit] Install deps"
#pip install -r requirements.txt
#
#cd ..
#git clone https://github.com/dominicprice/endplay
#cd endplay
#git submodule update --init --recursive
#pip install -e .

echo "[bridgeit] Exporting TG_TOKEN"
export TG_TOKEN="7976805123:AAHpYOm43hazvkXUlDY-q4X9US18upq9uak"

echo "[bridgeit] Run application"
python3 bot.py
