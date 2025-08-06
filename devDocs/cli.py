k='docs'
Y='utf-8'
X='README'
W=any
V=open
Q='.md'
N='.'
G=str
F=print
B=''
A=Exception
from logging import basicConfig as l,info as H,WARNING as Z,INFO,getLogger as a,exception as E
from os import listdir as m,getcwd as J,chdir as R,scandir as n,curdir as o,makedirs as S
from os.path import isdir as O,join as C,splitext as p,exists as b,getsize as q,dirname as c,abspath as d
from google.genai import Client as r
from google.genai.types import GenerateContentConfig as D
from argparse import ArgumentParser as s
from time import sleep
l(level=INFO)
a('google_genai').setLevel(Z)
a('httpx').setLevel(Z)
P='\nYou are Gantavya Bansal, a senior software engineer and expert technical writer. Your task is to generate clean, professional, and well-structured `README.md` documentation in Markdown format. Use the provided filename, source code, and any existing README or folder structure as context.\n\nYour output must be:\n\n- Concise and easy to follow\n- Focused on technical clarity and usability\n- Markdown-only (no extra commentary, no code fences)\n\nYour output must include:\n\n1. **Project Title** – Inferred from the filename or main script\n2. **Folder Structure** – Tree view if available, with clickable index links\n3. **Description** – What the project does and its purpose\n4. **How to Use** – Installation steps, CLI/API usage examples\n5. **Technologies Used** – Languages, tools, libraries\n6. **Architecture or Code Overview** – Key components, flow, functions, or classes\n7. **Known Issues / Improvements** – Current limitations, TODOs\n8. **Additional Notes or References** – Licensing, credits, related tools\n\nOnly return the final `README.md` content. Do not include any explanations, prefixes, or suffixes.\n\n                    '
def t(file,code,readme):
	J='```';I='```markdown';G=readme;F=code;C=file
	try:B=M.models.generate_content(model='gemini-2.0-flash-lite',config=D(system_instruction=P),contents=[f"Filename: {C}",f"Code:\n{F}",f"Existing README (if any):\n{G}"]);return B.text.removeprefix(I).removesuffix(J).strip()
	except A as H:
		try:B=M.models.generate_content(model='gemini-2.0-flash',config=D(system_instruction=P),contents=[f"Filename: {C}",f"Code:\n{F}",f"Existing README (if any):\n{G}"]);return B.text.removeprefix(I).removesuffix(J).strip()
		except A as H:
			try:B=M.models.generate_content(model='gemini-2.5-flash-lite',config=D(system_instruction=P),contents=[f"Filename: {C}",f"Code:\n{F}",f"Existing README (if any):\n{G}"]);return B.text.removeprefix(I).removesuffix(J).strip()
			except A as H:
				try:B=M.models.generate_content(model='gemini-2.5-flash',config=D(system_instruction=P),contents=[f"Filename: {C}",f"Code:\n{F}",f"Existing README (if any):\n{G}"]);return B.text.removeprefix(I).removesuffix(J).strip()
				except A as H:E(f"Error generating README for {C}: {H}");return f"# {C}\n\n⚠️ Failed to generate documentation GEMINI SERVER ERROR."
def e(start_path=N,prefix=B):
	L=prefix;D=start_path
	try:
		I=B;J=[];F=[]
		if not O(D):
			if O(c(d(D))):D=c(d(D))
			else:return B
		with n(D)as G:
			for H in G:
				if h(H.name):
					if H.is_dir():F.append(H.name)
					else:J.append(H.name)
		F.sort();J.sort();G=F+J
		for(N,K)in enumerate(G):
			P=C(D,K);M=N==len(G)-1;Q='└── 'if M else'├── ';I+=L+Q+K+'\n'
			if K in F:R='    'if M else'│   ';I+=e(P,L+R)
		return I
	except A as S:E(f"Error generating Tree for {D} dir: {S}");return f"# {D}\n\n⚠️ Failed to generate documentation tree."
def u(base,folders,files):
	I=files;G=folders;D=base
	try:
		F=K(D);F+=f"\n {e(start_path=D)} \n"
		if G:
			for L in G:O=C(J(),L);F+=f"\n readme for folder:{L} \n content inside: \n {K(O)} \n"
		if I:
			for M in I:F+=f"\n readme for file:{M} \n content inside: {K(M)} \n"
		f(X if D==N else D,F,K(X if D==N else D));H(B)
	except A as P:E(f"Error generating README for {D}: {P}")
def K(file):
	C=file
	try:
		if b(C+Q):
			with V(C+Q,'r',encoding=Y)as D:return D.read()
		else:return B
	except A as F:E(f"Error reading README for {C}: {F}");return f"# {C}\n\n⚠️ Failed to read {C}.md"
def v(file):
	B=file
	try:
		with V(B,'r',encoding=Y)as C:return C.read()
	except A as D:E(f"Error reading code in {B}: {D}");return f"# {B}\n\n⚠️ Failed to read {B}"
def f(file,code,readme):
	N='README.md';G=readme;D=file
	try:
		O=J().replace(U,B).lstrip('\\/').replace('\\','/');K=C(U,I,O);S(K,exist_ok=True);L=p(D)[0]+Q
		if X in L.upper():
			if not j:H('skipping overwriting README');M=C(K,N)
			else:M=C(N)
		else:M=C(K,L)
		G=i+G
		with V(M,'w',encoding=Y)as P:P.write(t(D,code,G))
		F(f"Written to: {L}")
	except A as R:E(f"Error writing README for {D}: {R}")
L=['cache','node','module','pkg','package','@','$','#','&','util','hook','component','python','compile','dist','build','env',k,'lib','bin','obj','out','__pycache__','.next','.turbo','.expo','.idea','.vscode','coverage','test','tests','fixtures','migrations','assets','static','logs','debug','config','style']
w=[N,'-','_','~']
T=['.log','.png','.jpg','.jpeg','.svg','.ico','.gif','.webp','.pyc','.class','.zip','.min.js','.mp4','.mp3','.wav','.pdf','.docx','.xlsx','.db','.sqlite','.bak','.7z','.rar','.tar.gz','.exe','.dll','.so','.ttf','.woff','.eot','.swp','.map','.webm',Q,'.css']
def g(base):
	I=base
	try:
		R(I);F(f"Reading Folder: {I}");N=[A for A in m()if h(A)];L=[A for A in N if O(C(J(),A))]
		if L:
			F('Folders found:')
			for D in L:H(D)
			for D in L:H(B);F(f"Opening Folder: {D}");g(D);F(f"Closing Folder: {D}");H(B)
		M=[A for A in N if not O(C(J(),A))and q(A)<1000000]
		if M:
			F('Files found:')
			for G in M:H(G)
			for G in M:P=v(G);Q=K(G);f(G,P,Q)
		u(I,L,M);R('..')
	except A as S:E(f"Failed to read {I} folder.")
def x(include,exclude):
	D=exclude;C=include
	try:
		C=[A.strip()for A in C.split(',')if A.strip()];D=[A.strip()for A in D.split(',')if A.strip()]
		for F in C:L.append(F.strip())
		for B in D:
			if B in L:L.remove(B.strip())
			if B in T:T.remove(B.strip())
	except A as G:E('Error in use with args --include  || --exclude')
def h(entry):A=entry.lower();return not W(A.startswith(B)for B in w)and not W(A.endswith(B)for B in T)and not W(B in A for B in L)
def y():
	try:
		C=s(description='Auto-generate documentation from source code and folder structure.');C.add_argument('-p','--path',type=G,default=N,help='Root path to scan (default: current directory)');C.add_argument('--name',type=G,default='My Project',help='Project name to include in README');C.add_argument('--description',type=G,default='No description provided.',help='Short description of the project');C.add_argument('--authors',type=G,default='Anonymous',help='Comma-separated list of author names');C.add_argument('--keywords',type=G,default=B,help='Comma-separated keywords (e.g., cli, docs, auto)');C.add_argument('--overwrite',action='store_true',help='Overwrite existing README files (default: False)');C.add_argument('--output',type=G,default=k,help='Output dir where docs to be stored (default: docs)');C.add_argument('--exclude',type=G,default=B,help='Folders, files, extensionse to exclude ((e.g., docs, ext, setting, config)');C.add_argument('--include',type=G,default=B,help='Folders, files, extensionse to include ((e.g., docs, ext, setting, config)');global M;global U;global I;global i;global j;D=C.parse_args();U=J();j=D.overwrite;I=D.output;x(include=D.include,exclude=D.exclude)
		if not b(I):S(I)
		L.append(I);i=f"name: {D.name}\ndescription: {D.description}\nauthors: {D.authors}\nkeywords: {D.keywords}";M=r(api_key=input('Paste your Google Gemini API Key here:').strip());F(f"📁 Starting in: {D.path}");S(I,exist_ok=True);R(D.path);g(o);F('✅ Documentation generated successfully.')
	except A as H:E('Error during execution. Try using --help.')
if __name__=='__main__':y()