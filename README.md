# ARIC — public website

Thai-first (EN toggle) one-page site of MFU-ARIC, the AI & Robotics Innovation Center at Mae Fah Luang University.
Live: https://cnacha-mfu.github.io/aric/

Static HTML/CSS/JS, no build step. The hero and the engine section draw the faceted ARIC mark live on canvas
(seeded Delaunay mesh; tap to add a point, the reseed chip regenerates it). Respects `prefers-reduced-motion`.

Contact and social values are placeholders: fill the `CONTACT` object at the top of the script in `index.html`
(empty values render as marked placeholders; the sign-up form sends by email to `CONTACT.email`).
Source copy in the IIC workspace: `website/`.
