"""Start webappen uansett hvilken mappe du står i:  python ipr-kurs/kjor.py"""
import os
import sys
from pathlib import Path

ROT = Path(__file__).resolve().parent
sys.argv[0] = str(Path(__file__).resolve())  # Flask-omstart maa finne fila etter chdir
os.chdir(ROT)
sys.path.insert(0, str(ROT))

from kurs.web.app import main  # noqa: E402

main(omstart=False)  # automatisk omstart finner ikke fila fra en annen mappe på Windows
