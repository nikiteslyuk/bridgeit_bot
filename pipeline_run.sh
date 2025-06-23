python3 -m venv venv

source venv/bin/activate

pip install -r requirements.txt

cd ..
git clone https://github.com/dominicprice/endplay
cd endplay
git submodule update --init --recursive

pip install -e .

export TG_TOKEN="7976805123:AAHpYOm43hazvkXUlDY-q4X9US18upq9uak"