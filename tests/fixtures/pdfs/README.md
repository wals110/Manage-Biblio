# PDFs de test pour l'intégration

Déposez ici 3 à 5 petits fichiers PDF pour les tests d'intégration.
Les noms doivent contenir des mots-clés reconnaissables, par exemple :

- `Introduction to Machine Learning - Author.pdf`
- `Linear Algebra and Applications - Author.pdf`
- `Quantum Mechanics Fundamentals - Author.pdf`
- `Python Programming Guide - Author.pdf`
- `Electronics Circuit Design - Author.pdf`

Les tests mockent l'API LLM Vision mais utilisent le vrai pipeline
de classification (theme_mapping + keywords). Le nom du fichier est
la source principale de classification dans ces tests.
