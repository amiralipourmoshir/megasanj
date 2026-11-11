# Hall display banners

Generated with the built-in image generation tool. No API/CLI package was installed.
Both PNGs are bundled with the desktop app and switch with its light/dark theme.
The official logo and Persian captions are drawn by Qt rather than generated inside the photo.
The ready truck's live Iranian plate is also drawn by Qt and projected onto the
original bumper corners using `hall_plate.py`; the base photographs are unchanged.
The public plate layout (yellow/black, blue country band, two digits + letter +
three digits + two-digit region) follows the format documented by the ANPR vendor:
https://industrialcortex.ai/countries/iran/

## Dark banner prompt

Use case: photorealistic-natural. Asset type: finished vertical photographic banner for the LEFT quarter of an industrial truck weighbridge queue display. Generate ONE portrait image, aspect ratio 1:3 approximately. Scene: a believable modern steel recycling factory entrance in Iran, a clean ordinary white European-style semitrailer truck with dark blue open-top steel scrap trailer, stationary on a long flush-mounted road weighbridge, grounded realistic scale platform, restrained corrugated metal factory architecture behind. Three-quarter front view, entire truck identifiable in middle-lower area, natural proportions, ordinary real factory, no people. Premium commercial architectural photography, simple composed image with realistic materials and restrained contrast. DARK THEME version: blue-hour dusk, deep ink navy #081426 shadows, cool slate and steel blue tones, subtle realistic warm cabin/factory lights, soft highlights, dark but subject clearly visible. Upper quarter quiet deep navy negative space for an interface heading placed later; lower edge quiet dark area. No printed text, no letters, no numerals, no logos, no watermark, no UI, no borders. Avoid science fiction, holograms, neon strips, futuristic trucks, floating objects, excessive machinery, excessive smokestacks, exaggerated sparks. This should feel like a real premium industrial photo, understated and beautiful, not a fantasy illustration. Save the generated image for use in the current project.

## Light banner prompt (edit of dark photograph)

Use case: lighting-weather. Edit target: attached tall portrait photo of white semi truck with a blue scrap trailer on a real steel factory weighbridge. Create its LIGHT THEME counterpart as a separate finished vertical photographic banner. Preserve the truck, scale platform, buildings, camera framing, vertical 1:3 aspect ratio and physical realism. Change only the time of day and photographic color grade to bright soft overcast daylight, quiet pale blue-gray sky, clean pearl white and muted steel blue palette, subtle natural sun highlights, neutral believable concrete ground, no glowing lamps necessary in daylight. Upper quarter remains uncluttered pale sky for a later text overlay. Premium restrained industrial architectural photography, understated, fresh, realistic. No text, no lettering, no logo, no UI, no watermarks; no futuristic objects or fantasy. Output a separate light version without changing the dark original.
