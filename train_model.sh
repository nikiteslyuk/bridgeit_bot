yolo train \
  model=yolov8s_playing_cards.pt \
  data=/Users/nikiteslyuk/Desktop/data/data.yaml \
  epochs=150 \
  imgsz=640 \
  batch=16 \
  patience=20 \
  device=mps \
  project=card_finetune \
  name=finetune_mps
