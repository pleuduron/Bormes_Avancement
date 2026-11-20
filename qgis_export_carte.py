# -*- coding: utf-8 -*-
"""Bormes — Export de la carte interactive des plaques vers GitHub Pages.

Depuis la console Python de QGIS :
exec(open(r"C:/Users/c.linares.GEOSATING/Documents/Bormes_Avancement/qgis_export_carte.py", encoding="utf-8").read())

L'export ne modifie pas les couches QGIS.
Seules les coordonnées WGS84 et le statut sont publiés.
"""
import json
import math
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from qgis.core import (
    QgsProject, QgsWkbTypes, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsLayerTreeLayer
)
from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox

CLE_AUTORISATION_CARTE = 'BormesAvancement/GitHubPublication/ConsentementCarteCoordonneesV1'


def _trouver_couches():
    racine = QgsProject.instance().layerTreeRoot()
    groupe = racine.findGroup('DONNE TERRAIN COMPLETE')
    if groupe is None:
        raise RuntimeError('Groupe introuvable : DONNE TERRAIN COMPLETE')
    terrain = [node.layer() for node in groupe.findLayers()
               if node.layer() and node.layer().name() == 'REGARD_GST']
    manquants = [node.layer() for node in racine.children()
                 if isinstance(node, QgsLayerTreeLayer)
                 and node.layer() and node.layer().name() == 'PLAQUES_MANQUANT']
    if len(terrain) != 1:
        raise RuntimeError('Une seule couche REGARD_GST doit se trouver dans DONNE TERRAIN COMPLETE. Trouvées : {}'.format(len(terrain)))
    if len(manquants) != 1:
        raise RuntimeError('Une seule couche PLAQUES_MANQUANT doit se trouver à la racine du projet. Trouvées : {}'.format(len(manquants)))
    for couche in (terrain[0], manquants[0]):
        if not couche.isValid():
            raise RuntimeError('Couche QGIS non valide : ' + couche.name())
        if QgsWkbTypes.geometryType(couche.wkbType()) != QgsWkbTypes.PointGeometry:
            raise RuntimeError('La couche {} doit contenir des points.'.format(couche.name()))
        if not couche.crs().isValid():
            raise RuntimeError('SCR non valide : ' + couche.name())
    return terrain[0], manquants[0]


def _geojson(terrain, manquants):
    projet = QgsProject.instance()
    destination = QgsCoordinateReferenceSystem('EPSG:4326')
    resultats = []
    compte = {'relevee': 0, 'manquante': 0}
    ignores = 0
    for couche, statut in ((terrain, 'relevee'), (manquants, 'manquante')):
        transformation = QgsCoordinateTransform(couche.crs(), destination, projet)
        for objet in couche.getFeatures():
            geometrie = objet.geometry()
            if geometrie.isNull() or geometrie.isEmpty():
                ignores += 1
                continue
            points = geometrie.asMultiPoint() if geometrie.isMultipart() else [geometrie.asPoint()]
            for point in points:
                try:
                    p = transformation.transform(point)
                    lon, lat = float(p.x()), float(p.y())
                    if not (math.isfinite(lon) and math.isfinite(lat)
                            and -180 <= lon <= 180 and -90 <= lat <= 90):
                        ignores += 1
                        continue
                except Exception:
                    ignores += 1
                    continue
                resultats.append({
                    'type': 'Feature',
                    'geometry': {'type': 'Point', 'coordinates': [round(lon, 7), round(lat, 7)]},
                    'properties': {'statut': statut}
                })
                compte[statut] += 1
    if not resultats:
        raise RuntimeError('Aucun point valide dans les deux couches. Publication annulée.')
    donnees = {
        'type': 'FeatureCollection',
        'metadata': {
            'updated_at': datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
            'source_relevees': 'DONNE TERRAIN COMPLETE / REGARD_GST',
            'source_restantes': 'PLAQUES_MANQUANT',
            'crs': 'EPSG:4326',
            'relevees': compte['relevee'],
            'manquantes': compte['manquante'],
            'geometries_ignorees': ignores
        },
        'features': resultats
    }
    return donnees, compte, ignores


def _autoriser_publication(compte):
    # Confirmation une seule fois par poste, distincte des statistiques.
    settings = QSettings()
    if settings.value(CLE_AUTORISATION_CARTE, False, type=bool):
        return True
    texte = (
        'Ce dépôt GitHub est PUBLIC.\n\n'
        'Les coordonnées précises des plaques seront librement accessibles '
        'et téléchargeables dans plaques.geojson.\n\n'
        'Plaques relevées (orange) : {relevee}\n'
        'Plaques restantes (noir) : {manquante}\n\n'
        'Confirmez-vous la publication de ces positions géographiques ?'
    ).format(**compte)
    autorise = QMessageBox.question(
        None, 'Autorisation des coordonnées publiques', texte,
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No
    ) == QMessageBox.Yes
    if autorise:
        settings.setValue(CLE_AUTORISATION_CARTE, True)
    return autorise


def _repo():
    emplacement = Path.home() / 'Documents' / 'Bormes_Avancement'
    if emplacement.is_dir() and (emplacement / '.git').exists():
        return emplacement
    choix = QFileDialog.getExistingDirectory(
        None, 'Choisir le dépôt local Bormes_Avancement',
        str(Path.home() / 'Documents')
    )
    if not choix:
        raise RuntimeError('Sélection du dépôt annulée.')
    return Path(choix)


def _publier(donnees, repo):
    # Réutilisation des fonctions de vérification Git existantes, sans
    # modifier la liste des fichiers autorisés du rapport statistique.
    sys.path.insert(0, str(repo))
    import bormes_publicacion as publication
    exe = publication._git_exe()
    repo = publication._valider_repo(exe, repo)
    fichier = 'plaques.geojson'
    cible = repo / fichier
    def git(*args, **kwargs):
        return publication._git(exe, repo, *args, **kwargs)
    if git('status', '--porcelain', '--', fichier).stdout.strip():
        raise RuntimeError('plaques.geojson contient des modifications locales. Publication annulée.')
    git('pull', '--ff-only', 'origin', 'main', timeout=120)
    if git('status', '--porcelain', '--', fichier).stdout.strip():
        raise RuntimeError('Conflit local après le pull. Vérifiez plaques.geojson.')
    contenu = json.dumps(donnees, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n'
    fd, temporaire = tempfile.mkstemp(prefix='_plaques_', suffix='.geojson', dir=str(repo))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as sortie:
            sortie.write(contenu)
        os.replace(temporaire, cible)
    finally:
        if os.path.exists(temporaire):
            os.remove(temporaire)
    git('add', '--', fichier)
    difference = git('diff', '--cached', '--quiet', '--', fichier, tolerate=(0, 1))
    if difference.returncode == 1:
        git('commit', '--only', '-m', 'Actualiser la carte des plaques depuis QGIS',
            '--', fichier)
    git('push', 'origin', 'main', timeout=180)


def actualiser_automatiquement(repo):
    """Actualise la carte après une publication réussie depuis QGIS.

    Retourne False si l'autorisation a été refusée. Toute exception est
    remontée au module appelant pour signaler l'échec séparément.
    """
    terrain, manquants = _trouver_couches()
    donnees, compte, ignores = _geojson(terrain, manquants)
    if not _autoriser_publication(compte):
        return False
    _publier(donnees, Path(repo))
    return True


def main():
    try:
        terrain, manquants = _trouver_couches()
        donnees, compte, ignores = _geojson(terrain, manquants)
        if not _autoriser_publication(compte):
            QMessageBox.information(None, 'Carte', 'Publication annulée. Les couches QGIS restent inchangées.')
            return
        _publier(donnees, _repo())
        QMessageBox.information(
            None, 'Carte publiée',
            '{} plaques relevées et {} plaques restantes publiées.\n'
            '{} géométries ignorées.\n\n'
            'https://pleuduron.github.io/Bormes_Avancement/#avancement'.format(
                compte['relevee'], compte['manquante'], ignores)
        )
    except Exception as erreur:
        QMessageBox.critical(None, 'Carte — erreur de publication', str(erreur))
        raise


if __name__ == '__main__':
    main()
