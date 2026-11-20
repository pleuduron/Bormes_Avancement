# -*- coding: utf-8 -*-
"""Publication de métriques agrégées sur GitHub Pages depuis les scripts QGIS.

Copier ce fichier à la racine du dépôt local Bormes_Avancement.
Il publie les indicateurs JSON et, après succès, les coordonnées des plaques autorisées.
Il ne publie jamais les autres couches QGIS, fichiers Excel ou CSV Khélios.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

REPO_ATTENDU = 'pleuduron/Bormes_Avancement'
SITE_URL = 'https://pleuduron.github.io/Bormes_Avancement/'
FICHIERS_AUTORISES = {'avancement.json': 'avancement', 'production.json': 'production'}
CLE_AUTORISATION = 'BormesAvancement/GitHubPublication/ConsentementPublicV8ParJour'
CLE_GIT_EXE = 'BormesAvancement/GitHubPublication/GitExeV7'


def _git_exe(parent=None):
    from qgis.PyQt.QtCore import QSettings
    from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox
    reg = QSettings()
    guess = [reg.value(CLE_GIT_EXE, '', type=str), shutil.which('git'), shutil.which('git.exe')]
    for base in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'),
                 os.environ.get('LOCALAPPDATA')):
        if base:
            guess += [str(Path(base) / 'Git' / p / 'git.exe') for p in ('cmd', 'bin')]
    for entry in guess:
        if entry and (Path(entry).is_file() or shutil.which(entry)):
            return entry
    result = QMessageBox.question(parent, 'Git introuvable',
        'QGIS ne trouve pas git.exe. Voulez-vous sélectionner Git for Windows ?',
        QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
    if result != QMessageBox.Yes:
        raise RuntimeError('Git introuvable. Vérifiez votre installation Git for Windows.')
    path, _ = QFileDialog.getOpenFileName(parent, 'Sélectionner git.exe',
        r'C:\Program Files\Git\cmd', 'Git for Windows (git.exe)')
    if not path or Path(path).name.lower() != 'git.exe':
        raise RuntimeError('Sélection de git.exe annulée.')
    reg.setValue(CLE_GIT_EXE, path)
    return path


def _git(exe, repo, *args, timeout=120, tolerate=(0,)):
    opts = {'capture_output': True, 'text': True, 'encoding': 'utf-8', 'errors': 'replace',
            'timeout': timeout, 'stdin': subprocess.DEVNULL}
    if os.name == 'nt':
        opts['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        res = subprocess.run([str(exe), '-C', str(repo), *args], **opts)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise RuntimeError('Échec de Git ({}): {}'.format(' '.join(args), err))
    if res.returncode not in tolerate:
        detail = (res.stderr or res.stdout or 'Erreur inconnue').strip()
        raise RuntimeError('git {} :\n{}'.format(' '.join(args), detail))
    return res


def _valider_repo(exe, repo):
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise ValueError('Dossier GitHub introuvable : ' + str(repo))
    racine = Path(_git(exe, repo, 'rev-parse', '--show-toplevel').stdout.strip()).resolve()
    if racine != repo:
        raise ValueError('Sélectionnez la racine du dépôt, pas un sous-dossier : ' + str(racine))
    branche = _git(exe, repo, 'branch', '--show-current').stdout.strip()
    if branche != 'main':
        raise ValueError('La branche Git locale doit être main, actuelle : ' + branche)
    remote = _git(exe, repo, 'remote', 'get-url', 'origin').stdout.strip()
    r = remote.replace('\\', '/').lower().removesuffix('.git').rstrip('/')
    if not (r.endswith('github.com/pleuduron/bormes_avancement') or
            r.endswith('github.com:pleuduron/bormes_avancement')):
        raise ValueError('Le dépôt origin ne correspond pas à ' + REPO_ATTENDU + ' : ' + remote)
    return repo


def _consentement(parent=None):
    from qgis.PyQt.QtCore import QSettings
    from qgis.PyQt.QtWidgets import QMessageBox
    s = QSettings()
    if s.value(CLE_AUTORISATION, False, type=bool):
        return True
    rep = QMessageBox.question(parent, 'Publication publique sur GitHub Pages',
        'Vous êtes sur le point de publier des indicateurs agrégés sur un site PUBLIC.\n\n'
        'Le tableau publie également des indicateurs PAR JOUR : date, linéaire, '
        'plaques et total d’heures, sans identité des techniciens.\n'
        'Aucun fichier de terrain ni CSV Khélios ne sera envoyé.\n'
        'Les coordonnées de plaques peuvent être publiées séparément,\n'
        'avec votre accord spécifique.\n\n'
        'Confirmez-vous avoir l’autorisation de publier ces indicateurs ?',
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    if rep != QMessageBox.Yes:
        return False
    s.setValue(CLE_AUTORISATION, True)
    return True


def _actualiser_carte_apres_publication(repo, parent=None):
    """Lance la mise à jour cartographique, sans compromettre le JSON publié.

    Le fichier GeoJSON n'est envoyé qu'après vérification des deux couches,
    consentement cartographique indépendant et succès de la publication JSON.
    """
    from qgis.PyQt.QtWidgets import QMessageBox
    module_path = Path(repo) / 'qgis_export_carte.py'
    try:
        if not module_path.is_file():
            raise RuntimeError(
                'Module qgis_export_carte.py introuvable dans le dépôt local. '
                'Exécutez git pull origin main avant le prochain traitement.'
            )
        spec = importlib.util.spec_from_file_location('bormes_carte_auto', str(module_path))
        if spec is None or spec.loader is None:
            raise RuntimeError('Chargement du module cartographique impossible.')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.actualiser_automatiquement(repo)
    except Exception as erreur:
        # La publication du rapport a déjà réussi : ne pas annoncer un échec global.
        QMessageBox.warning(
            parent,
            'Indicateurs publiés — carte non actualisée',
            'Les indicateurs ont bien été publiés sur GitHub, mais la carte '
            'n’a pas pu être actualisée.\n\n' + str(erreur)
        )
        return False


def publier_json(repo, fichier, donnees, parent=None):
    """Met à jour un seul JSON, en préservant le second, puis commit et push.

    Retour : URL du site si push réussi, None si publication refusée.
    Lève une erreur explicite si Git échoue ; le rapport Excel n'est jamais touché.
    Une erreur du GeoJSON est signalée séparément, sans annuler le JSON réussi.
    """
    if fichier not in FICHIERS_AUTORISES:
        raise ValueError('Fichier de publication non autorisé : ' + fichier)
    if not isinstance(donnees, dict) or donnees.get('source') != FICHIERS_AUTORISES[fichier]:
        raise ValueError('Schéma de données incompatible pour ' + fichier)
    if donnees.get('schema_version') != 1:
        raise ValueError('Version du schéma incompatible : version 1 attendue')
    if not _consentement(parent):
        return None
    exe = _git_exe(parent)
    repo = _valider_repo(exe, repo)
    cible = repo / fichier
    if _git(exe, repo, 'status', '--porcelain', '--', fichier).stdout.strip():
        raise RuntimeError(f'{fichier} possède déjà des modifications locales non publiées. '
                           'Vérifiez Git avant une nouvelle publication (git status).')
    # Reprend les changements de GitHub avant toute écriture locale.
    _git(exe, repo, 'pull', '--ff-only', 'origin', 'main', timeout=120)
    # Vérifie à nouveau après le pull, pour éviter de remplacer une modification locale.
    if _git(exe, repo, 'status', '--porcelain', '--', fichier).stdout.strip():
        raise RuntimeError('Modifications locales détectées après git pull : ' + fichier)
    contenu = json.dumps(donnees, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    fd, temp = tempfile.mkstemp(prefix='_bormes_', suffix='.json', dir=str(repo))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(contenu)
        os.replace(temp, cible)
    finally:
        if os.path.exists(temp):
            os.remove(temp)
    _git(exe, repo, 'add', '--', fichier)
    diff = _git(exe, repo, 'diff', '--cached', '--quiet', '--', fichier, tolerate=(0, 1))
    if diff.returncode == 1:
        _git(exe, repo, 'commit', '--only', '-m',
             'Actualiser {} depuis QGIS ({})'.format(fichier, datetime.now().strftime('%Y-%m-%d %H:%M')),
             '--', fichier)
    # Si la donnée est identique, un push peut quand même terminer un commit précédent.
    _git(exe, repo, 'push', 'origin', 'main', timeout=180)
    _actualiser_carte_apres_publication(repo, parent)
    return SITE_URL
