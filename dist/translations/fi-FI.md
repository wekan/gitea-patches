# fi-FI

Translate the Gitea web UI completely into Finnish.

Upstream's locale_fi-FI.json had about 1400 of the 4000+ strings of
locale_en-US.json, so most of the web UI fell back to English. The missing
strings are now in Finnish, using the terms the existing translation already
used: repo, ongelma (issue), vetopyyntö (pull request), haara, tunniste
(label), merkkipaalu, pääsymerkki (access token).

Some existing strings are corrected too:

- "Pull requestit" / "Pull-pyynnöt" became "vetopyyntö", the word the rest of
  the file uses.
- repo.issues.review.comment said "Kommentoi" while English is "reviewed %s",
  so the review time link was missing; now "tarkisti %s".
- Terms used throughout: runner = ajo, review = tarkistus, reviewer =
  tarkistaja, blame = syyllinen, SSH principal = SSH-päätoimija. "Review" in
  the sense of checking something says tarkista, and "preview" esikatselu.
- Product names of webhook types and quoted labels of third-party sites stay
  in English on purpose.

Every printf verb and HTML tag matches en-US; apply-translations.py leaves out
any string where they do not.

Lower confidence, worth a native review: adopt = omaksua, claim = väite,
scoped workflows = rajatut työnkulut, and the activity summary fragments
(repo.activity.git_stats_*) whose word order is joined at runtime.

Merged by key by gitea-patches' releases/apply-translations.py: a key this
translation does not have keeps upstream's Finnish string.
