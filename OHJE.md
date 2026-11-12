# meetnotes: käyttöohje

meetnotes kirjoittaa muistiinpanot Zoom-, Teams- ja Google Meet -kokouksistasi.
Se toimii kokonaan omalla koneellasi. Kokoukseen ei liity mitään botti-osallistujaa.

## Mitä se tekee lyhyesti

1. Se huomaa kokouksen alkaneen ikkunan otsikosta, esimerkiksi `Zoom Meeting`, `Meet – abc-defg-hij` tai `Viikkopalaveri | Microsoft Teams`.
2. Se nauhoittaa kaksi ääntä erikseen: mikrofonisi (sinä) ja sen, mikä soi kuulokkeissasi (muut).
3. Whisper muuttaa puheen tekstiksi omalla koneellasi, 30 sekunnin pätkinä kokouksen aikana.
4. Kun kokousikkuna sulkeutuu, Claude kirjoittaa muistiinpanot: yhteenvedon, päätökset, tehtävät vastuuhenkilöineen, sinun jatkotoimesi ja avoimet kysymykset.

```
mikrofoni ─────────────┐                          ┌─ transcript.md  (kokouksen aikana)
                       ├─ pätkät ─ Whisper ─ siivous ─┤
kuulokkeiden ääni ─────┘    (oma kone)                └─ Claude ─ notes.md (kokouksen jälkeen)
(loopback)
```

Ääni ei koskaan lähde koneeltasi. Vain valmis teksti lähetetään Anthropicin API:in muistiinpanojen kirjoittamista varten.

## Lataaminen ja päivittäminen

Meetnotes julkaistaan GitHubissa. Jokaisessa julkaisussa on:

| Tiedosto | Kenelle |
|---|---|
| `Meetnotes-Windows.zip` | Windows: pura ja aja `windows\Asenna Meetnotes.cmd` |
| `Meetnotes-macOS.zip` | Mac: pura ja tuplaklikkaa `mac/Asenna Meetnotes.command` |
| `Meetnotes.apk` | Android-puhelin |
| `meetnotes-x.y.z-py3-none-any.whl` | sovelluksen koodi, jonka päivitys lataa itse |

**Päivitys ei vaadi uutta asennusta.** Sovellus tarkistaa kerran päivässä, onko uusi versio. Kotisivulle tulee ilmoitus "Versio x.y.z on saatavilla", ja **Asetukset → Päivitykset → Päivitä nyt** hoitaa loput:
1. Se lataa vain ohjelman, noin 0,3 Mt. Python, puhemalli ja kirjastot pysyvät koneella.
2. Se tarkistaa tiedoston SHA-256-tarkistussumman ennen asennusta.
3. Se odottaa, että käynnissä oleva kokous päättyy ja sen muistiinpanot on kirjoitettu.
4. Taustapalvelu käynnistyy uudelleen uudella versiolla, ja sovellusikkuna lataa sivun uudestaan.

Asetukset, API-avain, kalenteri, valmistelut ja muistiinpanot säilyvät. Päivityksen voi tehdä myös komentorivillä: `meetnotes update` (tai `meetnotes update --check`).

**Android päivittyy tietokoneen mukana.** Puhelinsovellus on ikkuna tietokoneen sovellukseen, joten uudet sivut ja toiminnot tulevat puhelimeen heti tietokoneen päivityksen jälkeen. Itse APK päivitetään harvoin. Silloin Asetukset-sivulle tulee "Android-sovelluksesta on uusi versio" ja Lataa-nappi. Koska uusi APK on allekirjoitettu samalla avaimella, Android asentaa sen vanhan päälle, ja pariliitos säilyy.

**Julkaisijalle:** `python release.py --owner <github-tunnus>` rakentaa kaikki yllä olevat tiedostot ja `update.json`-tiedoston kansioon `dist/`. Ne ladataan GitHub-julkaisuun (Release) tagilla `v<versio>`. Mukana on myös GitHub Actions -työnkulku, joka tekee tämän automaattisesti, kun tagi työnnetään. Asennetut kopiot etsivät päivityksiä osoitteesta `https://github.com/<tunnus>/meetnotes/releases/latest/download/update.json`, joka on leivottu pakettiin julkaisun yhteydessä. Pidä `android/meetnotes.keystore` tallessa ja salassa: ilman samaa avainta Android ei asenna APK-päivitystä vanhan päälle.

## Mac-sovellus

1. Pura `Meetnotes-macOS.zip` ja tuplaklikkaa `mac/Asenna Meetnotes.command`.
2. Jos macOS sanoo "ei voida avata, koska kehittäjää ei voida tunnistaa", napsauta tiedostoa Ctrl-näppäin pohjassa, valitse **Avaa** ja vahvista.
3. Asennus etsii Pythonin (python.org, Homebrew tai Xcode). Jos sopivaa ei ole, se avaa latausivun.
4. Kuulokkeiden ääntä varten asennetaan BlackHole. Luo sen jälkeen *Ääni-MIDI-asetuksissa* usean ulostulon laite (kuulokkeet + BlackHole) ja valitse se ulostuloksi.
5. Asennus luo **Meetnotes.app**-sovelluksen kansioon `~/Applications`. Vedä se Dockiin.
6. Anna macOS:n kysyessä mikrofoni- ja käyttöapuoikeudet. Käyttöapu tarvitaan kokousikkunoiden tunnistamiseen.

Poisto: `mac/Poista Meetnotes.command`. Muistiinpanot jäävät kansioon `~/MeetingNotes`.

## Windows-sovellus (suositus)

1. **Poista latausmerkintä ennen purkamista.** Napsauta tiedostoa `meetnotes.zip` hiiren oikealla painikkeella ja valitse **Ominaisuudet**. Ruksaa alareunasta **Poista esto** (Unblock) ja paina **OK**. Ilman tätä Windows 11:n Smart App Control estää asennuksen viestillä "blocked a file that may be unsafe".
2. Pura zip: hiiren oikea painike, **Pura kaikki** ja **Pura**.
3. Avaa puretun kansion alikansio `windows` ja tuplaklikkaa **Asenna Meetnotes.cmd**.
4. Asennus hoitaa kaiken itse, eikä se vaadi järjestelmänvalvojan oikeuksia:
   - Se asentaa Pythonin, jos koneelta ei löydy sopivaa.
   - Se asentaa sovelluksen ja lataa suomen puheentunnistusmallin, mikä vie ensimmäisellä kerralla muutaman minuutin.
   - Se luo **Meetnotes-kuvakkeen työpöydälle** ja Käynnistä-valikkoon.
   - Se kytkee taustapalvelun käynnistymään aina, kun kirjaudut Windowsiin.
5. Sovellus aukeaa itsestään. Lisää **Asetukset**-sivulla Claude API -avain. Avain tallentuu Windowsin tunnistetietojen hallintaan, ei tiedostoon.

Asennus tekee valmiiksi suomenkieliset asetukset: puhuttu kieli suomi, muistiinpanot suomeksi ja tarkka `large-v3-turbo`-malli.

**Sovelluksessa on neljä sivua:**
- **Koti:** tila (odottaa, nauhoittaa, tauolla), *Nauhoita nyt*, *Tauko*, *Lopeta nauhoitus* ja automaattisen nauhoituksen kytkin.
- **Kokoukset:** kaikki kokoukset, muistiinpanot, koko teksti ja poisto.
- **Kysy:** kysy Claudelta mitä tahansa aiemmista kokouksistasi.
- **Asetukset:** puhelimen yhdistäminen, API-avain, kieli, turvasana ja yksityisyys.

Ikkunan sulkeminen rastista vain piilottaa sen. Nauhoitus jatkuu taustalla, ja ikkunan saa takaisin työpöydän kuvakkeesta tai ilmaisinalueen kuvakkeesta tehtäväpalkin oikeasta reunasta. Ilmaisinalueen kuvakkeen valikosta voi myös aloittaa ja lopettaa nauhoituksen.

Poisto tapahtuu tiedostolla `windows\Poista Meetnotes.cmd`. Muistiinpanot jäävät talteen kansioon `MeetingNotes`.

## Android-sovellus

Androidille on oma sovellus, tiedosto `Meetnotes.apk`. Se näyttää saman Meetnotesin kuin tietokone, mutta toimii kuin tavallinen sovellus:
- Sillä on oma kuvake.
- Varmennevaroitusta ei tule.
- Mikrofonilupa kysytään kerran.
- Näyttö pysyy päällä puhelinnauhoituksen ajan.

**Asennus**
1. Lataa `Meetnotes.apk` puhelimeen ja avaa se.
2. Android kysyy lupaa asentaa sovelluksia tästä lähteestä, esimerkiksi Chromesta tai Tiedostot-sovelluksesta. Salli se.
3. Play Protect voi varoittaa tuntemattomasta kehittäjästä, koska sovellus ei tule Play-kaupasta. Valitse **Lisätietoja → Asenna silti**.

**Yhdistäminen**
1. Avaa tietokoneella Meetnotes ja valitse **Asetukset → Puhelin**. Kytke puhelinyhteys päälle ja valitse **Android-sovellus**.
2. Ruudulla näkyy osoite (esim. `192.168.1.20`) ja kuusinumeroinen koodi.
3. Avaa puhelimessa Meetnotes, syötä osoite ja koodi ja paina **Yhdistä**.

Koodi toimii kerran ja vaihtuu 10 minuutin välein. Viisi väärää yritystä lukitsee yhdistämisen minuutiksi. Yhdistäessään sovellus tallentaa tietokoneesi varmenteen sormenjäljen ja hyväksyy sen jälkeen vain sen tietokoneen.

Toiseen tietokoneeseen yhdistetään sovelluksen kohdasta **Asetukset → Yhdistä toiseen tietokoneeseen**. Jos asennat tietokoneen Meetnotesin kokonaan uudelleen, sen varmenne vaihtuu. Silloin sovellus pyytää yhdistämään uudelleen.

iPhonessa käytetään selainversiota: **Asetukset → Puhelin → Selain (iPhone)**.

## Puhelinsovellus

Puhelimella on kaksi käyttötapaa.

1. **Tietokoneen kaukosäädin:** kytke automaattinen nauhoitus päälle tai pois, aloita tai lopeta nauhoitus tietokoneella, pidä tauko, lue muistiinpanot ja kysy kokouksista.
2. **Nauhoita tällä puhelimella:** kasvokkaisiin kokouksiin. Puhelimen mikrofoni kuulee koko huoneen. Ääni siirtyy tietokoneelle viiden sekunnin pätkinä nauhoituksen aikana, joten mitään ei katoa, vaikka yhteys katkeaisi hetkeksi. Tietokone tekee tekstin ja muistiinpanot, ja ne aukeavat puhelimeen, kun ne ovat valmiit. Turvasana toimii tässäkin: kenen tahansa huoneessa sanoma "omerta" lopettaa kuuntelun, koska puhelimen mikrofoni ei erota puhujia.

**Yhdistäminen kerran:**
1. Avaa tietokoneella Meetnotes ja valitse **Asetukset → Puhelin** ja kytke **Puhelinyhteys** päälle.
2. Jos Windows kysyy palomuurilupaa, valitse **Yksityiset verkot** ja Salli.
3. Skannaa ruudulla näkyvä QR-koodi puhelimen kameralla.
4. Selain varoittaa yhteydestä kerran, koska suojausvarmenne on tietokoneesi oma eikä ostettu. Valitse "Lisätiedot" ja sitten "Jatka".
5. Lisää sivu aloitusnäytölle. iPhonessa: Jaa → Lisää Koti-valikkoon. Androidissa: ⋮ → Lisää aloitusnäytölle. Sen jälkeen Meetnotes aukeaa puhelimessa kuin mikä tahansa sovellus.

**Rajoitukset, jotka on hyvä tietää:**
- Puhelin ja tietokone pitää olla **samassa Wi-Fi-verkossa**, ja tietokoneen pitää olla päällä.
- **Puhelin ei voi nauhoittaa Teams-, Zoom- tai Meet-puhelua, joka on auki puhelimessa itsessään.** iPhone ja Android estävät muiden sovellusten puheluäänen kaappauksen kokonaan. Liity näihin kokouksiin tietokoneella, niin Meetnotes nauhoittaa ne.
- Puhelimella nauhoittaessa pidä näyttö päällä. Sovellus pyytää puhelinta pitämään näytön päällä, mutta iPhone saattaa silti pysäyttää nauhoituksen, jos lukitset sen.

## Asennus komentoriviltä (macOS, Linux tai Windows ilman asennusohjelmaa)

Tarvitset Python 3.11:n tai uudemman ja Anthropic API -avaimen (console.anthropic.com).

```bash
pip install "/polku/meetnotes[app]"
meetnotes app        # avaa saman sovellusikkunan
```

Aseta API-avain pysyvästi.

- macOS/Linux: lisää rivi `export ANTHROPIC_API_KEY=sk-ant-...` tiedostoon `~/.zshrc` tai `~/.bashrc`.
- Windows: `setx ANTHROPIC_API_KEY sk-ant-...` ja avaa uusi pääte.

Sen jälkeen:

```bash
meetnotes setup
```

### Kuulokkeiden äänen kaappaus

Kuulokkeet ovat paras vaihtoehto. Mikrofoniin ei vuoda muiden puhetta, joten "Minä" ja "Muut" erottuvat siististi.

**Windows.** Mitään ei tarvitse asentaa. meetnotes nauhoittaa suoraan sen laitteen, joka on Windowsin oletustoisto: USB-, Bluetooth- tai johdolliset kuulokkeet. Huolehdi vain siitä, että kokoussovelluksen kaiutinasetus on "Sama kuin järjestelmä" tai että kuulokkeet ovat Windowsin oletuslaite.

**macOS.** macOS ei anna nauhoittaa toistoääntä suoraan, joten tarvitaan ilmainen BlackHole-ajuri.
1. `brew install blackhole-2ch portaudio`
2. Avaa *Audio MIDI -asetukset* ja paina vasemmasta alakulmasta **+** ja sitten *Luo usean ulostulon laite*.
3. Valitse ensin kuulokkeesi ja sitten BlackHole 2ch. Kuulokkeiden on oltava ylimpänä, eli kellolähteenä. Ruksaa BlackHolen kohdalta *Drift Correction*.
4. Valitse tämä usean ulostulon laite äänen ulostuloksi (Järjestelmäasetukset > Ääni).
5. Kirjoita asetustiedostoon `system_device = "BlackHole"`.
6. Kokoussovelluksessa kaiuttimeksi "Sama kuin järjestelmä".

Huomaa, että macOS ei salli usean ulostulon laitteen äänenvoimakkuuden säätöä näppäimistöltä. Säädä äänenvoimakkuus kokoussovelluksesta.

**Linux.** Toimii automaattisesti PulseAudion tai PipeWiren kautta. Se nauhoittaa sen ulostulon, joka oikeasti soi, myös Bluetooth-kuulokkeet. Asenna tarvittaessa `sudo apt install libportaudio2 pulseaudio-utils wmctrl`.

### Testaa ennen ensimmäistä kokousta

```bash
meetnotes check-audio
```

Laita esimerkiksi YouTube-video soimaan kuulokkeisiin ja puhu samalla. Näet kymmenen sekunnin ajan kaksi mittaria, `me` ja `others`. Lopuksi komento kertoo, kuuluvatko molemmat puolet. Jos `others` on hiljaa, kokoussovellus tai selain soittaa ääntä laitteeseen, jota meetnotes ei kuuntele. Korjaa se kohdan 6 mukaan.

Lopuksi `meetnotes doctor` tarkistaa kaiken kerralla: äänen, Whisperin, API-avaimen ja palvelun.

## Käynnistys

### Suositus: taustalle, käynnistyy itse

```bash
meetnotes start          # käynnistää taustalle nyt
meetnotes autostart on   # käynnistää taustalle aina kun kirjaudut koneelle
```

Tämän jälkeen sinun ei tarvitse tehdä mitään. Taustapalvelulla ei ole ikkunaa, kuvaketta eikä ilmoituksia. Se odottaa kokousta, nauhoittaa sen ja kirjoittaa muistiinpanot.

| Komento | Mitä tekee |
|---|---|
| `meetnotes status` | Kertoo, onko palvelu käynnissä ja nauhoittaako se juuri nyt. |
| `meetnotes pause` | Lopettaa nauhoituksen heti. Ääni hävitetään, eikä sitä tallenneta tai litteroida. |
| `meetnotes resume` | Jatkaa nauhoitusta. Tauko loppuu myös itsestään, kun kokous päättyy. |
| `meetnotes stop` | Pysäyttää palvelun. Käynnissä olevan kokouksen muistiinpanot kirjoitetaan ensin. |
| `meetnotes autostart off` | Poistaa automaattisen käynnistyksen. |

### Käsin yksittäiseen kokoukseen

```bash
meetnotes record --title "Asiakaspalaveri"     # Ctrl-C lopettaa
meetnotes record --minutes 45                   # lopettaa itse 45 minuutin kuluttua
meetnotes record --show                         # näyttää tekstin livenä tässä ikkunassa
```

## Kalenteri

Lisää kalenterisi ICS-osoite kohtaan **Asetukset → Kalenteri**:
- Google: Kalenterin asetukset → Integroi kalenteri → *Salainen osoite iCal-muodossa*.
- Outlook / Microsoft 365: Asetukset → Kalenteri → Jaetut kalenterit → Julkaise kalenteri → ICS-linkki.

Sen jälkeen:
- **Seuraavat kokoukset** näkyvät kotisivulla ja Avustaja-sivulla.
- **Valmistele**-nappi tekee valmistelun suoraan kalenterimerkinnästä: nimi, aika, osallistujat ja kutsun numeroidut tai luetellut rivit agendaksi.
- **Kokoukset nimetään kalenterin mukaan.** "Zoom Meeting" on jatkossa "Kontio Oy – sopimusneuvottelu", ja osallistujien nimet annetaan muistiinpanojen kirjoittajalle.

Kalenteria vain luetaan. Osoite tallennetaan asetustiedostoon, joten älä jaa sitä: Googlen salainen osoite näyttää koko kalenterisi kenelle tahansa, jolla osoite on.

## Omat muistiinpanot kokouksen aikana

Kotisivulla on kokouksen aikana **Omat muistiinpanot** -kenttä. Kirjoita siihen vapaasti puhelimella tai tietokoneella: nimiä, lukuja, "muista kysyä tuesta". Kokouksen jälkeen Claude yhdistää omat muistiinpanosi tekstiin ja käsittelee niitä luotettavimpana tietona siitä, mikä sinulle oli tärkeää. Ne näkyvät myös kokouksen **Omat**-välilehdellä.

## Kokoustyypit

Muistiinpanoissa on aina yhteenveto, keskeiset asiat, päätökset, tehtävät, omat jatkotoimet ja avoimet kysymykset. Kokouksen tyyppi lisää niihin omat osionsa:

| Tyyppi | Lisäosiot |
|---|---|
| Myynti / asiakas | Asiakkaan tarpeet, Vastaväitteet ja huolet, Hinta ja ehdot, Kilpailijat, Kaupan tilanne |
| 1:1 | Kuulumiset, Palaute, Tavoitteet ja kehittyminen, Seuraavalla kerralla |
| Standup / tilannekatsaus | Edistyminen, Esteet, Riskit aikataululle |
| Haastattelu | Tausta, Konkreettiset esimerkit, Vahvuudet, Huolet, Kandidaatin kysymykset |
| Projektikokous | Laajuus ja muutokset, Aikataulu ja budjetti, Riskit ja riippuvuudet |
| Hallitus / johtoryhmä | Viralliset päätökset, Siirretyt asiat |

Oletus on **Automaattinen**, jolloin Claude tunnistaa tyypin itse. Tyypin voi valita valmistelussa, kokouksen aikana kotisivulta tai pysyvästi asetuksista. Omia tyyppejä voi lisätä asetustiedostoon:

```toml
[[templates]]
id = "retro"
name = "Retrospektiivi"
instructions = "Keskity siihen, mikä meni hyvin ja mitä muutetaan."
sections = ["Mikä meni hyvin", "Mitä muutetaan", "Kokeilut"]
```

## Jatkuvuus, tehtävät ja tilastot

- **Viime kerrasta.** Kun kokouksella on sama nimi kuin aiemmalla, esimerkiksi viikkopalaveri, muistiinpanot kertovat jokaisesta edellisen kerran tehtävästä ja avoimesta kysymyksestä, tehtiinkö se, onko se yhä auki vai eikö siitä puhuttu.
- **Tehtävät-sivu** kokoaa kaikkien kokousten tehtävät ja jatkotoimesi. Rastita tehdyt. Avoimet tehtäväsi annetaan avustajalle seuraavan valmistelun pohjaksi, ja muiden tehtävät silloin, kun ne liittyvät samoihin ihmisiin.
- **Aiheet.** Muistiinpanoissa on aikaleimattu aihelista. Aiheen napautus vie tekstissä tai videossa oikeaan kohtaan.
- **Tilastot:** puheaika minä/muut, kysymysten määrä, pisin monologi ja puhetahti. Lasketaan koneella ilman API-kutsua.
- **Avainsanat.** Asetuksiin kirjoitetut sanat, kuten "kilpailija" tai "alennus", nousevat kotisivun syötteeseen heti, kun joku sanoo ne. Tämäkään ei maksa mitään.
- **Jaa.** Kokouksen sivulla **Kopioi** vie muistiinpanot leikepöydälle ja **Sähköposti** avaa sähköpostiluonnoksen. **Webhook-osoite** asetuksissa lähettää muistiinpanot JSON-muodossa jokaisen kokouksen jälkeen esimerkiksi Zapieriin tai n8n:ään, josta ne saa CRM:ään tai Slackiin.

## Avustaja: valmistelu ja reaaliaikainen tuki

Avustaja on sovelluksen **Avustaja**-sivulla. Se toimii kolmessa vaiheessa, jotka jakavat saman valmistelun.

**1. Ennen kokousta.** Luo valmistelu: kokouksen nimi, ajankohta, agenda, tavoitteet, osallistujat ja materiaalit, kuten sähköpostit tai tarjous. Avustaja hakee itse aiemmat kokoukset, joissa sama nimi tai osallistujat mainitaan, ja kirjoittaa briiffin: mitä aiemmin on sovittu, mitkä asiat ovat auki, mitä ottaa esiin, mitä kysyä ja mitä varoa. Voit myös kysyä siltä suoraan, esimerkiksi "Mitä Kontion kanssa on sovittu hinnasta?" Vastaus kertoo, mistä kokouksesta tieto on peräisin.

**2. Kokouksen aikana.** Kun kokous alkaa, avustaja tunnistaa valmistelun päivämäärästä ja kokouksen nimestä. Kotisivulle ilmestyy Avustaja-paneeli, jossa näkyvät agendan eteneminen ja ehdotukset:
- **Ota esiin:** asia, jonka olet valmistelussa tai aiemmassa kokouksessa luvannut, eikä kukaan ole maininnut sitä.
- **Kysy:** kysymys, joka kannattaa esittää nyt.
- **Huomio:** juuri sanottu asia on ristiriidassa materiaalien tai aiemman kokouksen kanssa.
- **Päätös** ja **Tehtävä:** mitä juuri sovittiin, jotta voit vahvistaa sen.

Paneelissa on myös kysymyskenttä. Puhelin on tähän paras, koska muut eivät näe sitä. Jos valmistelua ei tunnistettu, voit liittää sen paneelin valikosta. Avustajan voi kytkeä päälle myös kesken kokouksen, ja se saa silloin siihenastisen tekstin.

**3. Kokouksen jälkeen.** Muistiinpanot kertovat, mitkä agendan kohdat käsiteltiin ja mitkä jäivät. Ehdotukset tallentuvat kokouksen kansioon tiedostoon `assistant.md`.

**Miten se toimii.** Koko kokous on yksi jatkuva keskustelu Clauden kanssa. Noin 45 sekunnin välein avustaja saa vain uudet rivit, ei koko tekstiä uudelleen. Siksi se ei toista ehdotuksiaan, ja aiempi osa tulee välimuistista, mikä pitää hinnan pienenä. Tunnin kokous maksaa noin 0,5–1 € Anthropicin API-hinnoilla. Vain teksti ja valmistelu lähtevät koneelta.

**Asetukset** (Asetukset → Avustaja kokouksen aikana):
- **Avustaja päällä kokouksissa:** käynnistyy itse jokaisessa kokouksessa. Oletuksena pois.
- **Tyyli:** hiljainen vastaa vain kysymyksiin ja merkitsee päätökset. Tasapainoinen antaa enintään kaksi ehdotusta päivityksessä. Aktiivinen ehdottaa enemmän.
- **Päivitysväli:** 30 sekunnista 2 minuuttiin.
- **Malli:** Claude Opus 5.5 on tarkin. Sonnet 5.5 on nopeampi ja noin puolet halvempi. Valmistelu ja muistiinpanot käyttävät aina Opus 5.5:tä.

Puhelimella nauhoitetuissa kasvokkaisissa kokouksissa avustaja ei toimi reaaliajassa, koska teksti tehdään vasta nauhoituksen jälkeen. Valmistelu ja kysymykset toimivat niissäkin.

## Näytön tallennus

Kytke päälle sovelluksen **Asetukset → Näytön tallennus → Tallenna myös näyttö**. Sen jälkeen jokaisesta kokouksesta tallentuu ääniraidan lisäksi video näytöstäsi.

- **Video ja ääni:** video tallentuu kokouksen kansioon nimellä `screen.mp4`, ja siinä on mukana kokouksen ääni. Sen voi katsoa sovelluksessa kokouksen **Video**-välilehdeltä myös puhelimella tai avata millä tahansa videosoittimella.
- **Kuvataajuus:** oletuksena tallennetaan 5 kuvaa sekunnissa, ja kuva pienennetään 1280 pikselin levyiseksi. Kokous on enimmäkseen paikallaan pysyvää kuvaa, joten tämä riittää. Tunti vie noin 0,2–0,5 Gt.
- **Näyttö:** valittavana ovat päänäyttö, toinen näyttö tai kaikki näytöt yhdessä.
- **Tauko ja turvasana:** tauon aikana video on musta. Turvasanan jälkeen tallennus loppuu, ja video leikataan samaan kohtaan kuin ääni.
- **Kertanauhoitus:** komentorivillä `meetnotes record --screen` tallentaa näytön yhdestä nauhoituksesta.

**Näkyvyys.** Kaappaus tehdään Windowsin tavallisella kuvakaappausrajapinnalla. Siitä ei tule keltaista reunaa näytölle, eikä kokoussovellus tai muut osallistujat voi havaita sitä.

**Kaksi rajoitusta:**
- Jos järjestäjä on kytkenyt Teamsin **näyttökuvien eston** päälle (Teams Premium, "Prevent screen capture"), kokousikkuna näkyy videolla mustana. Ääni ja muistiinpanot tallentuvat silti normaalisti.
- Videossa näkyvät muiden osallistujien kasvot ja jaetut esitykset. Samat säännöt koskevat sitä kuin äänitallennetta: tallenteen voi pitää itsellään, mutta sen jakaminen muille vaatii harkintaa.

## Turvasana "omerta"

Kun sanot kesken kokouksen **"omerta"**, kuunteleminen loppuu siihen kokoukseen.

- Kaikki sanan jälkeen sanottu poistetaan: tekstistä, nauhoitteesta ja muistiinpanoista. Myös se sinun lauseesi, jossa sana oli, jää pois.
- Sitä ennen sanottu säilyy, ja muistiinpanot tehdään siitä tavalliseen tapaan. Itse sanaa ei mainita muistiinpanoissa.
- Vain sinun mikrofonisi voi lopettaa nauhoituksen. Jos joku muu kokouksessa sanoo "omerta", mitään ei tapahdu.
- Seuraava kokous nauhoitetaan taas normaalisti. Jos haluat jatkaa nauhoitusta samassa kokouksessa, aja `meetnotes stop` ja sitten `meetnotes start`.
- Tunnistus sietää eri ääntämykset ja kirjoitusasut, kuten "omerta", "omertà", "omertaa" ja "o merta". Arkisanat kuten "merta" (meri), "omena" ja "omat" eivät laukaise sitä.

**Vinkki: muut eivät kuule, jos mykistät itsesi ensin.** Kokoussovelluksen mykistys (Zoom, Teams, Meet) estää vain sen, että ääni lähtee kokoukseen. meetnotes kuulee mikrofonisi silti. Mykistä itsesi, sano "omerta" ja poista mykistys. Tämä ei toimi, jos mykistät mikrofonin laitteesta, kuulokkeiden johdon napista tai käyttöjärjestelmästä.

**Viive.** Sana huomataan, kun sen sisältänyt noin 30 sekunnin pätkä on litteroitu. Kuunteleminen loppuu siis viimeistään noin puolen minuutin kuluttua. Sen välin ääni poistetaan jälkikäteen, eikä se päädy tekstiin tai muistiinpanoihin missään vaiheessa. Lyhyempi `chunk_seconds = 15` tekee tunnistuksesta nopeamman.

**Asetukset** (`~/.config/meetnotes/config.toml`):

```toml
safe_word = "omerta"         # oma sana, tai "" jos haluat ominaisuuden pois
safe_word_action = "stop"    # "stop" = muistiinpanot sanaa edeltävästä osasta
                             # "discard" = koko kokous poistetaan, mitään ei jää
```

Tarkista tila komennolla `meetnotes status`. Turvasanan jälkeen se kertoo, että nauhoitus pysäytettiin turvasanalla. Mitään ilmoitusta tai ääntä ei tule, jotta mikään ei näy tai kuulu muille.

## Muistiinpanojen käyttö

Jokaisesta kokouksesta syntyy kansio, esimerkiksi `~/MeetingNotes/2026-10-29_1500_Viikkopalaveri/`. Kansion voi avata vain oma käyttäjätilisi.

| Tiedosto | Sisältö |
|---|---|
| `notes.md` | Muistiinpanot |
| `transcript.md` | Koko keskustelu aikaleimoin, `Me:` = sinä, `Others:` = muut |
| `audio.wav` | Nauhoite, vasen kanava = sinä, oikea = muut. Poista käytöstä: `keep_audio = false`. |
| `live-notes.md` | Kokouksen aikana päivittyvät muistiinpanot, jos `live_notes = true` |

```bash
meetnotes list                                   # viimeisimmät kokoukset
meetnotes open                                   # avaa uusimmat muistiinpanot
meetnotes search budjetti                        # hakee sanaa kaikista kokouksista
meetnotes ask "Mitä Sami lupasi tehdä tällä viikolla?"   # Claude vastaa kokoushistoriasi perusteella
meetnotes summarize ~/MeetingNotes/2026-10-29_1500_Viikkopalaveri   # kirjoittaa muistiinpanot uudelleen
meetnotes recover                                # tekee muistiinpanot kokouksista, jotka katkesivat
```

## Toimintamekaniikka tarkemmin

**Kokouksen tunnistus.** Palvelu lukee näkyvien ikkunoiden otsikot viiden sekunnin välein. Nauhoitus alkaa, kun kokousikkuna näkyy kahdesti peräkkäin. Teamsin tavallinen pääikkuna, kuten `Chat | Microsoft Teams`, ei käynnistä nauhoitusta. Itse kokoussovelluksiin ei kosketa.

**Lopetus.** Nauhoitus loppuu, kun kokousikkuna on ollut poissa 45 sekuntia (`end_grace_seconds`). Lyhyt katkos tai ikkunan pienentäminen ei siis katkaise sitä. Nauhoitus loppuu myös, jos kukaan ei ole puhunut 15 minuuttiin (`idle_stop_minutes`). Tämä estää turhan nauhoituksen, jos esimerkiksi Teamsin ikkuna jää auki kokouksen jälkeen.

**Pätkät.** Ääni litteroidaan noin 30 sekunnin pätkinä. Pätkä katkaistaan lähimmästä hiljaisesta kohdasta, ettei sana katkea kesken.

**Kuka puhuu.** Rivit ovat `Me:` (sinä) ja `Others:` (kaikki muut). Oletuksena ääni litteroidaan kerran yhdistettynä, ja jokainen lause merkitään sille, jonka kanavalla ääni oli kovempi. Tämä on puolet kevyempää kuin kanavien litterointi erikseen, ja kuulokkeilla yhtä tarkkaa. Asetuksista voi valita tarkan tilan, jossa kanavat litteroidaan erikseen. Muita osallistujia ei erotella toisistaan, mutta Claude päättelee nimet keskustelusta, kun ne mainitaan.

**Kaiunpoisto.** Jos käytät kaiuttimia, mikrofoni kuulee myös muut. Toisen kanavan kanssa päällekkäiset toistot poistetaan automaattisesti (`dedupe_echo`). Kuulokkeilla tätä ei tarvita, mutta siitä ei ole haittaakaan.

**Turvasana.** Mikrofonin teksti tarkistetaan jokaisesta pätkästä ennen kuin mitään tallennetaan tekstitiedostoon. Kun "omerta" löytyy, sen kohdasta eteenpäin kaikki jätetään pois, uusia pätkiä ei enää litteroida, ja nauhoite leikataan siihen kohtaan. Jos Whisper arvioi pätkän todennäköisesti muuksi kuin puheeksi, se ei lopeta nauhoitusta. Näin hiljaisuudesta keksitty sana ei voi katkaista kokousta vahingossa.

**Tauko.** `meetnotes pause` hävittää äänen siihen asti, kunnes jatkat. Tekstiin tulee merkintä `(recording paused)`, eikä Claude arvaile, mitä tauon aikana sanottiin.

**Tyhjät nauhoitukset.** Jos kokouksessa sanottiin alle 20 sanaa, kansio poistetaan, eikä muistiinpanoja tehdä.

**Kaatumiset.** Teksti tallentuu levylle koko ajan. Jos kone sammuu kesken kokouksen, palvelu tekee muistiinpanot puuttuvista kokouksista seuraavalla käynnistyskerralla.

## Suomenkieliset kokoukset

Laita asetustiedostoon (`~/.config/meetnotes/config.toml`):

```toml
language = "fi"                 # ei arvailua suomen ja englannin välillä
whisper_model = "large-v3-turbo"  # paljon parempi suomessa kuin "small"
notes_language = "Finnish"      # muistiinpanot aina suomeksi
vocabulary = ["Sami", "Kesko", "Kubernetes"]   # nimet ja termit oikein kirjoitettuina
ignore_titles = ["1:1", "kehityskeskustelu"]   # näitä ei nauhoiteta koskaan
```

`large-v3-turbo` on hitaampi kuin `small`. Uudella kannettavalla se pysyy yleensä reaaliajassa. Jos `meetnotes status` tai loki kertoo litteroinnin jäävän jälkeen, kokeile mallia `medium`.

## Näkyvyys: kuka voi tietää, että käytät tätä

Tarkistin tämän kohta kohdalta.

**Muut kokouksen osallistujat eivät näe mitään.** meetnotes ei liity kokoukseen, ei käytä Zoomin, Teamsin tai Googlen rajapintoja eikä lähetä niille mitään. Osallistujalistassa ei ole bottia, eikä mikään "nauhoitetaan"-ilmoitus syty. Teknisesti ei ole mitään, mistä muut voisivat sen havaita.

Jaa kuitenkin aina vain yksittäinen ikkuna, älä koko näyttöä. Muutama asia voi muuten näkyä jakamallesi näytölle:
- Meetnotes-ikkuna, jos se on auki. Sulje se rastista ennen jakamista, sillä nauhoitus jatkuu silti.
- Ilmaisinalueen kuvake tehtäväpalkissa. Sen saa pois asetuksesta **Kuvake ilmaisinalueella**.
- Jos ajat `meetnotes record` -komentoa päätteessä, se ikkuna voi näkyä. Taustapalvelulla ikkunaa ei ole.
- Työpöydän ilmoitukset ovat oletuksena pois (`notify = false`). Pidä ne pois, jos jaat näyttöä usein.
- Muistiinpanokansio, jos se on auki jaettavalla näytöllä.

macOS:n oranssi mikrofonipiste ja Windowsin mikrofonikuvake palavat kokouksen aikana joka tapauksessa, koska kokoussovellus käyttää mikrofonia. Ne näkyvät vain sinun näytölläsi.

**Puhelinyhteys.** Se on salattu ja toimii vain samassa Wi-Fi-verkossa. Sisään pääsee vain laite, jolla on QR-koodin sisältämä pitkä satunnainen avain. Älä siis jaa kuvaa QR-koodista. Kytke puhelinyhteys pois, kun et tarvitse sitä. Jos haluat mitätöidä kaikki yhdistetyt puhelimet, poista tiedosto `%LOCALAPPDATA%\meetnotes\app_token` ja käynnistä kone uudelleen. Silloin syntyy uusi koodi.

**Mitä en tee piiloon.** Jos kone on työnantajan, IT-osasto voi nähdä asennetut ohjelmat, käynnissä olevat prosessit ja verkkoliikenteen osoitteeseen `api.anthropic.com`. En ole rakentanut tähän mitään, mikä piilottaisi sen yrityksen tietoturvavalvonnalta, enkä tee sellaista. Jos käytät tätä työkoneella, tarkista ensin, mitä työpaikkasi ohjeet sanovat kokousten litteroinnista ja tekoälypalveluihin lähetettävästä tiedosta.

**Laki, lyhyesti (en ole lakimies).** Suomessa saa nauhoittaa keskustelun, jossa on itse osapuolena. Rikoslain salakuuntelupykälä koskee keskusteluja, joihin et osallistu. Tallenteen tai muistiinpanojen levittäminen muille voi kuitenkin loukata osallistujien yksityisyyttä. Työpaikan ohjeet, salassapitosopimukset ja asiakassopimukset voivat myös kieltää nauhoittamisen. Muissa maissa säännöt vaihtelevat. Esimerkiksi osassa Yhdysvaltain osavaltioista tarvitaan kaikkien osapuolten suostumus. Kun kokouksessa on ulkomaisia osallistujia, heidän sijaintinsa lait voivat koskea myös sinua.

## Vianetsintä

| Ongelma | Ratkaisu |
|---|---|
| Muut puuttuvat tekstistä | `meetnotes check-audio`, kokoussovelluksen kaiuttimeksi järjestelmän oletus (macOS: usean ulostulon laite) |
| Ei käynnisty kokouksessa | macOS: anna Pythonille tai Päätteelle *Käyttöapu*-oikeus (Järjestelmäasetukset > Tietosuoja). Linux: asenna `wmctrl`. |
| Väärin kirjoitettuja nimiä | lisää ne `vocabulary`-listaan |
| Huono suomi | `language = "fi"` ja `whisper_model = "large-v3-turbo"` |
| Muistiinpanot epäonnistuivat (API) | teksti säilyy, aja myöhemmin `meetnotes summarize <kansio>` |
| Mitä palvelu teki? | `meetnotes status` näyttää lokin sijainnin |
| "Viimeistellään tekstiä" kestää kauan | Kone litteroi vielä. Kotisivu näyttää, montako minuuttia ääntä on jäljellä. Nopeuta valitsemalla Asetuksista malli "Small" tai kytke näytön tallennus pois. |
| Kokouksella ei ole muistiinpanoja | Avaa kokous: virheen syy näkyy, ja **Yritä uudelleen** kirjoittaa ne uudestaan. |
| Kotisivulla "Nauhoitus ei käynnistynyt" | viesti kertoo syyn, usein mikrofoni puuttuu tai on toisen ohjelman varaama |
| Puhelin ei saa yhteyttä | sama Wi-Fi? puhelinyhteys päällä? palomuuriluvan "Yksityiset verkot"? |
| "Smart App Control blocked a file" | Poista latausmerkintä zipistä (Ominaisuudet → Poista esto) ja pura se uudelleen. Katso Windows-sovelluksen asennuksen kohta 1. |
| Sovellusikkuna ei aukea | `windows\Asenna Meetnotes.cmd` uudelleen asentaa WebView2:n. Ilman sitä sovellus aukeaa selaimeen. |
