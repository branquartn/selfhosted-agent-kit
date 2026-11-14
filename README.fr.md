# SelfHosted Agent Kit — Démarrage rapide (FR)

Agent IA auto-hébergé complet : chat web en streaming, outils réels (fichiers,
shell, recherche), mémoire longue durée, historique de sessions, API protégée par
jeton. **Zéro dépendance** : Python 3.10+ et c'est tout.

Compatible avec tout fournisseur au format OpenAI : DeepSeek, OpenAI, Groq,
OpenRouter, vLLM, LM Studio, Ollama.

## Installation (2 minutes)

```bash
./install.sh                  # crée .env, dossiers, jeton, et propose le service systemd
$EDITOR .env                  # renseigner LLM_API_KEY et LLM_MODEL
./run.sh                      # → http://127.0.0.1:8800
```

Ouvrir l'URL, coller le `AGENT_TOKEN` affiché par le script, écrire.

Avec Docker :

```bash
cp .env.example .env && $EDITOR .env
docker compose up -d
```

## Les 3 variables qui comptent

| Variable | Rôle |
|---|---|
| `LLM_BASE_URL` | endpoint compatible OpenAI (`https://api.deepseek.com/v1`, `http://127.0.0.1:11434/v1`…) |
| `LLM_API_KEY` | clé du fournisseur (peut rester vide pour Ollama local) |
| `AGENT_TOKEN` | secret de l'API — généré par `install.sh` |

## Ce que fait l'agent

- lit, écrit et cherche dans `workspace/` (sandbox : sortie de répertoire refusée) ;
- exécute des commandes shell (`AGENT_SHELL_ENABLED=false` pour couper,
  `AGENT_SHELL_ALLOW=git,ls` pour restreindre) ;
- **demande ton accord** avant toute écriture de fichier ou commande shell (boutons
  Autoriser / Refuser dans l'interface ; `AGENT_APPROVAL=auto` pour s'en passer) ;
- mémorise les faits durables dans `var/memory/*.md` — un fichier par souvenir,
  lisible et éditable à la main ;
- garde l'historique des conversations dans `var/sessions/*.json`.

## Sécurité — à lire

Le sandbox protège des erreurs, **pas d'un modèle hostile**. Lancer l'agent avec un
utilisateur UNIX non privilégié, idéalement dans un conteneur, sans accès à
`~/.ssh` ni aux identifiants cloud. Toujours derrière TLS.

## Licence

Code visible, **pas open source** : lecture, essai et usage personnel non commercial libres
(voir `LICENSE`). Tout usage professionnel ou client demande la licence commerciale
(`LICENSE-COMMERCIAL.md`), une organisation / un produit.
Support et mises à jour : 12 mois.
