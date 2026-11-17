# PDF to Kindle EPUB

Lokalny skrypt w Pythonie do konwersji PDF z warstwą tekstową na EPUB z regulowaną czcionką, spisem treści z zakładek PDF oraz ilustracjami. Ustawienia układu można dopasować argumentami CLI lub własnym profilem JSON.

Nie korzysta z modelu AI, klucza API ani zewnętrznej usługi. Konwersja odbywa się lokalnie. Repozytorium zawiera kod i profil układu; nie zawiera książki, jej fragmentów ani wygenerowanych e-booków.

## Instalacja

Python **3.12 lub nowszy**. Sprawdzono na Pythonie 3.12.

```bash
git clone https://github.com/JakubMlocek/pdf-to-kindle-epub.git
cd pdf-to-kindle-epub
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

W Windows aktywuj środowisko przez `.venv\Scripts\Activate.ps1` w PowerShell.

## Użycie

Ogólna konwersja prostego PDF:

```bash
python pdf_to_epub.py input.pdf output.epub \
  --title "Tytuł książki" \
  --author "Imię Nazwisko" \
  --language pl
```

Tytuł i autor są domyślnie pobierani z metadanych PDF. `--author` można podać wielokrotnie. Domyślny język to `en`; tekst nie jest tłumaczony.

Przykład własnego dopasowania:

```bash
python pdf_to_epub.py input.pdf output.epub \
  --top-margin 50 \
  --bottom-margin 31 \
  --cover-page 1 \
  --snapshot-pages "3-4,10" \
  --image-scale 3
```

- `--top-margin` i `--bottom-margin`: pomijany obszar w punktach PDF (72 punkty = 1 cal). W trybie ogólnym domyślnie 0; nagłówki stron mogą wymagać ręcznego ustawienia marginesów.
- `--snapshot-pages`: strony zachowane w całości jako obrazy, zamiast tekstu ze zmienną czcionką.
- `--cover-page`: strona użyta jako okładka. W trybie ogólnym nie wybiera się jej automatycznie.
- `--image-scale`: skala renderowania ilustracji; 3 oznacza 216 dpi. Mniejsza wartość zmniejsza rozmiar EPUB.
- `--no-tables`: wyłącza wykrywanie tabel z liniami.
- `--force`: pozwala zastąpić istniejący EPUB. Domyślnie istniejący plik jest chroniony.
- `--profile ścieżka.json`: własny profil, oparty na [profiles/example.json](profiles/example.json). Parametry CLI mają pierwszeństwo przed odpowiadającymi im ustawieniami profilu. Pola pominięte w JSON używają wartości trybu ogólnego. Numery stron liczone są od 1, według pozycji w PDF.

Wynik jest najpierw zapisywany do pliku tymczasowego i sprawdzany, a dopiero potem przenoszony do miejsca docelowego.

## Co skrypt robi

1. Odczytuje tekst, fonty, pozycje elementów i zakładki PDF przez PyMuPDF.
2. Łączy wiersze w akapity i heurystycznie rozpoznaje nagłówki oraz część list.
3. Zachowuje obrazy i wykryte tabele jako ilustracje. Własny profil może dodać rozpoznawanie fontów etykiet diagramów i obszarów pod podpisami.
4. Buduje sekcje i hierarchiczny spis treści z zakładek PDF. Bez zakładek powstaje jedna sekcja.
5. Tworzy EPUB przez EbookLib i sprawdza strukturę ZIP/XML oraz odwołania do zasobów i kotwic.

## Ograniczenia

- **Nie każdy PDF ma ten sam układ.** Profil ogólny jest punktem wyjścia; książki techniczne zwykle wymagają dopasowania i kontroli wyniku. Przykładowy profil pokazuje format ustawień i wymaga dostosowania do konkretnego PDF.
- Brak OCR. Skan może pozostać obrazem, ale nie otrzyma przeszukiwalnego tekstu ani regulowanej czcionki.
- Tabele bez linii, złożone kolumny, przypisy i diagramy wektorowe bez podpisów mogą wymagać profilu lub `--snapshot-pages`.
- Łączenie wyrazów podzielonych między wierszami jest heurystyczne. Akapity urwane na granicy stron mogą pozostać osobnymi akapitami.
- Obrazy, diagramy i strony zachowane w całości jako obrazy nie mają regulowanej wielkości czcionki.
- Kotwice spisu treści wskazują stronę źródłową zawierającą podrozdział; nie wyznaczają dokładnej pozycji nagłówka wewnątrz tej strony.
- Zachowane drukowane numery stron i odsyłacze nie są przeliczane na pozycje EPUB. Oryginalne hiperłącza PDF nie są odtwarzane.
- Kontrola struktury nie zastępuje EPUBCheck ani sprawdzenia wizualnego. Przed czytaniem przejrzyj kilka rozdziałów, pytania, tabele i diagramy w czytniku EPUB.

## Kindle

Wynikowy EPUB prześlij przez [Send to Kindle](https://www.amazon.com/sendtokindle). Jest to plik przeznaczony do konwersji przez tę usługę; skrypt nie tworzy AZW3/MOBI ani nie wysyła plików na konto Amazon.

## Testy

```bash
python -m unittest discover -s tests -v
```

Testy generują własny niewielki PDF i sprawdzają konwersję tekstu, ilustrację, spis treści, okładkę, strony zachowane jako obrazy, odwołania EPUB oraz ochronę istniejącego wyniku. Nie potrzebują książki źródłowej.
