Check your typing, in order

	1.	python -m py_compile impact.py. Silence means the syntax is right. Any error names the line to fix.
	2.	python impact.py FT. It should print a header showing origin/315-1 as the base, then "No changes."
	3.	python impact.py FT origin/<a recent feature branch> to see a real report.
	4.	python impact.py gate before your next production deploy.

Add > report.txt to any command to save the output.

Run the Crystal check on a real report early. The tool finds table names inside .rpt files by searching their bytes, and I can't confirm that works on your reports. If a changed table you know a report uses shows no match, send me 


No, the typed version isn't a replacement for the first one. It's a deliberately trimmed core, because you can't copy files in and the full script is about 400 lines, which is too error-prone to type by hand. Nothing needs generating until you've run it.

What the typed impact.py keeps

	•	PR impact report: changed files, tables/views/procs touched, other files that use them, and Crystal .rpt matches.
	•	Release gate: Hotfix commits missing from UAT, and it blocks on a wrong branch or failed fetch.
	•	Stale-base warning and the "diff against whatever branch is checked out in each folder" behaviour, so branch numbers aren't hardcoded.
	•	Read-only: it only runs git fetch and read-only git commands.

What I dropped from the full script

	•	The LLM plug-in and the --ping test.
	•	The JSON config file: the folder paths and the 21-day threshold now sit as constants at the top of the file.
	•	Report files in impact-reports\: use > report.txt instead.
	•	--all, --init, --probe and per-run base-branch overrides.
	•	Controller-to-React endpoint linking.
	•	The mapping/SQL drift warning, which was noisy anyway.

When I reread the full script, it already contained Crystal handling and the --probe option that I hadn't written in this conversation. I tested it before relying on it, and I rewrote the Crystal part more compactly in the short version.

Why the short version is the right first step
Everything the full script adds is polish except the LLM and endpoint linking, and both are better built after you've seen real output. Typing 133 lines is realistic. Four hundred isn't.

To protect your credits from here

	1.	Type it and run the four checks from my last message.
	2.	Send me the real output or the exact error line, plus the 3 code samples when you're ready.
	3.	I'll reply with small patches of a few lines to retype, not whole files. The LLM script will be a separate short file, added only when the model is cleared.

