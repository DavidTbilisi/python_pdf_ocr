import cv2
import os
import time
import pytesseract
from pdf2image import convert_from_path
exported_name = "10americans"


def convert_to_image():
    # pdfs = r"provide path to pdf file"
    pdfs = r"/home/arch1/Code/PYTHON/OCR_py/americans.pdf"
    pages = convert_from_path(pdfs, 200)

    i = 1
    for page in pages:
        image_name = "images/Page_" + str(i) + ".jpg"
        page.save(image_name, "JPEG")
        print(image_name, "converted and saved")
        i = i+1

def ocr_core(img):
    text = pytesseract.image_to_string(img, lang='kat')
    time.sleep(1)
    with open(f"/home/arch1/Code/PYTHON/OCR_py/{exported_name}.txt", "a", encoding="utf8") as f:
        f.write(text + " \n")
    return text




# convert_to_image()
DIR = '/home/arch1/Code/PYTHON/OCR_py/images'
final = len([name for name in os.listdir(DIR) if os.path.isfile(os.path.join(DIR, name))])
for page in range(1, final+1):
    img = cv2.imread(f"images/Page_{page}.jpg")
    print(f"\n-=Converting {page} page=-\n")
    print(ocr_core(img))

print()
