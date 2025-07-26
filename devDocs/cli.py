i='docs'
V='utf-8'
U='README'
T=any
S=open
C='.md'
M='.'
G=str
F=print
E=Exception
A=''
from logging import basicConfig as j,info as H,WARNING as W,INFO,getLogger as X,exception as D
from os import listdir as k,getcwd as J,chdir as O,scandir as l,curdir as m,makedirs as P
from os.path import isdir as N,join as B,splitext as n,exists as Y,getsize as o,dirname as Z,abspath as a
from google.genai import Client as p
from google.genai.types import GenerateContentConfig as q
from argparse import ArgumentParser as r
from time import sleep
j(level=INFO)
X('google_genai').setLevel(W)
X('httpx').setLevel(W)
def s(file,code,readme):
	A=file
	try:sleep(1);B=f.models.generate_content(model='gemini-2.0-flash-lite',config=q(system_instruction='\nYou are Gantavya Bansal, a senior software engineer and expert technical writer. Your task is to generate clean, professional, and well-structured `README.md` documentation in Markdown format. Use the provided filename, source code, and any existing README or folder structure as context.\n\nYour output must be:\n\n- Concise and easy to follow\n- Focused on technical clarity and usability\n- Markdown-only (no extra commentary, no code fences)\n\nYour output must include:\n\n1. **Project Title** – Inferred from the filename or main script\n2. **Folder Structure** – Tree view if available, with clickable index links\n3. **Description** – What the project does and its purpose\n4. **How to Use** – Installation steps, CLI/API usage examples\n5. **Technologies Used** – Languages, tools, libraries\n6. **Architecture or Code Overview** – Key components, flow, functions, or classes\n7. **Known Issues / Improvements** – Current limitations, TODOs\n8. **Additional Notes or References** – Licensing, credits, related tools\n\nOnly return the final `README.md` content. Do not include any explanations, prefixes, or suffixes.\n\n                    '),contents=[f"Filename: {A}",f"Code:\n{code}",f"Existing README (if any):\n{readme}"]);return B.text.removeprefix('```markdown').removesuffix('```').strip()
	except E as C:D(f"Error generating README for {A}: {C}");return f"# {A}\n\n⚠️ Failed to generate documentation GEMINI SERVER ERROR."
def b(start_path=M,prefix=A):
	L=prefix;C=start_path
	try:
		I=A;J=[];F=[]
		if not N(C):
			if N(Z(a(C))):C=Z(a(C))
			else:return A
		with l(C)as G:
			for H in G:
				if e(H.name):
					if H.is_dir():F.append(H.name)
					else:J.append(H.name)
		F.sort();J.sort();G=F+J
		for(O,K)in enumerate(G):
			P=B(C,K);M=O==len(G)-1;Q='└── 'if M else'├── ';I+=L+Q+K+'\n'
			if K in F:R='    'if M else'│   ';I+=b(P,L+R)
		return I
	except E as S:D(f"Error generating Tree for {C} dir: {S}");return f"# {C}\n\n⚠️ Failed to generate documentation tree."
def t(base,folders,files):
	I=files;G=folders;C=base
	try:
		F=K(C);F+=f"\n {b(start_path=C)} \n"
		if G:
			for L in G:O=B(J(),L);F+=f"\n readme for folder:{L} \n content inside: \n {K(O)} \n"
		if I:
			for N in I:F+=f"\n readme for file:{N} \n content inside: {K(N)} \n"
		c(U if C==M else C,F,K(U if C==M else C));H(A)
	except E as P:D(f"Error generating README for {C}: {P}")
def K(file):
	B=file
	try:
		if Y(B+C):
			with S(B+C,'r',encoding=V)as F:return F.read()
		else:return A
	except E as G:D(f"Error reading README for {B}: {G}");return f"# {B}\n\n⚠️ Failed to read {B}.md"
def u(file):
	A=file
	try:
		with S(A,'r',encoding=V)as B:return B.read()
	except E as C:D(f"Error reading code in {A}: {C}");return f"# {A}\n\n⚠️ Failed to read {A}"
def c(file,code,readme):
	O='README.md';K=readme;G=file
	try:
		Q=J().replace(R,A).lstrip('\\/').replace('\\','/');L=B(R,I,Q);P(L,exist_ok=True);M=n(G)[0]+C
		if U in M.upper():
			if not h:H('skipping overwriting README');N=B(L,O)
			else:N=B(O)
		else:N=B(L,M)
		K=g+K
		with S(N,'w',encoding=V)as T:T.write(s(G,code,K))
		F(f"Written to: {M}")
	except E as W:D(f"Error writing README for {G}: {W}")
L=['cache','node','module','pkg','package','@','$','#','&','util','hook','component','python','compile','dist','build','env',i,'lib','bin','obj','out','__pycache__','.next','.turbo','.expo','.idea','.vscode','coverage','test','tests','fixtures','migrations','assets','static','logs','debug','config','style']
v=[M,'-','_','~']
Q=['.log','.png','.jpg','.jpeg','.svg','.ico','.gif','.webp','.pyc','.class','.zip','.min.js','.mp4','.mp3','.wav','.pdf','.docx','.xlsx','.db','.sqlite','.bak','.7z','.rar','.tar.gz','.exe','.dll','.so','.ttf','.woff','.eot','.swp','.map','.webm',C,'.css']
def d(base):
	I=base
	try:
		O(I);F(f"Reading Folder: {I}");P=[A for A in k()if e(A)];L=[A for A in P if N(B(J(),A))]
		if L:
			F('Folders found:')
			for C in L:H(C)
			for C in L:H(A);F(f"Opening Folder: {C}");d(C);F(f"Closing Folder: {C}");H(A)
		M=[A for A in P if not N(B(J(),A))and o(A)<1000000]
		if M:
			F('Files found:')
			for G in M:H(G)
			for G in M:Q=u(G);R=K(G);c(G,Q,R)
		t(I,L,M);O('..')
	except E as S:D(f"Failed to read {I} folder.")
def w(include,exclude):
	C=exclude;B=include
	try:
		B=[A.strip()for A in B.split(',')if A.strip()];C=[A.strip()for A in C.split(',')if A.strip()]
		for F in B:L.append(F.strip())
		for A in C:
			if A in L:L.remove(A.strip())
			if A in Q:Q.remove(A.strip())
	except E as G:D('Error in use with args --include  || --exclude')
def e(entry):A=entry.lower();return not T(A.startswith(B)for B in v)and not T(A.endswith(B)for B in Q)and not T(B in A for B in L)
def x():
	try:
		B=r(description='Auto-generate documentation from source code and folder structure.');B.add_argument('-p','--path',type=G,default=M,help='Root path to scan (default: current directory)');B.add_argument('--name',type=G,default='My Project',help='Project name to include in README');B.add_argument('--description',type=G,default='No description provided.',help='Short description of the project');B.add_argument('--authors',type=G,default='Anonymous',help='Comma-separated list of author names');B.add_argument('--keywords',type=G,default=A,help='Comma-separated keywords (e.g., cli, docs, auto)');B.add_argument('--overwrite',action='store_true',help='Overwrite existing README files (default: False)');B.add_argument('--output',type=G,default=i,help='Output dir where docs to be stored (default: docs)');B.add_argument('--exclude',type=G,default=A,help='Folders, files, extensionse to exclude ((e.g., docs, ext, setting, config)');B.add_argument('--include',type=G,default=A,help='Folders, files, extensionse to include ((e.g., docs, ext, setting, config)');global f;global R;global I;global g;global h;C=B.parse_args();R=J();h=C.overwrite;I=C.output;w(include=C.include,exclude=C.exclude)
		if not Y(I):P(I)
		L.append(I);g=f"name: {C.name}\ndescription: {C.description}\nauthors: {C.authors}\nkeywords: {C.keywords}";f=p(api_key=input('Paste your Google Gemini API Key here:').strip());F(f"📁 Starting in: {C.path}");P(I,exist_ok=True);O(C.path);d(m);F('✅ Documentation generated successfully.')
	except E as H:D('Error during execution. Try using --help.')
if __name__=='__main__':x()