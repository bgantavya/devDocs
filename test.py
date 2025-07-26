M='docs'
L='python'
K=print
F='README.md'
E='utf-8'
D=open
C=True
Q=['cache','node','module','pkg','package','@','$','#','&','util','hook','component',L,'compile','dist','build','env',M,'lib','bin','obj','out','__pycache__','.next','.turbo','.expo','.idea','.vscode','coverage','test','tests','fixtures','migrations','assets','static','logs','debug','config','style']
R=['.','-','_','~']
S=['.log','.png','.jpg','.jpeg','.svg','.ico','.gif','.webp','.pyc','.class','.zip','.min.js','.mp4','.mp3','.wav','.pdf','.docx','.xlsx','.db','.sqlite','.bak','.7z','.rar','.tar.gz','.exe','.dll','.so','.ttf','.woff','.eot','.swp','.map','.webm','.md','.css']
import os as A,shutil as G,unittest as H
from unittest.mock import patch as I,MagicMock as N
import subprocess as O
B='test_project'
J=M
P='def hello_world():\n    print("Hello, world!")'
class T(H.TestCase):
	def setUp(H):
		A.makedirs(B,exist_ok=C)
		with D(A.path.join(B,'main.py'),'w',encoding=E)as G:G.write(P)
		with D(A.path.join(B,F),'w',encoding=E)as G:G.write('# Sample Project\n\nThis is a sample readme.')
	def tearDown(D):
		G.rmtree(B,ignore_errors=C);G.rmtree(J,ignore_errors=C)
		if A.path.exists(F):A.remove(F)
	@I('builtins.input',lambda*A:'FAKE_API_KEY')
	@I('google.genai.Client')
	def test_documentation_generation(self,mock_client):
		F=N();F.models.generate_content.return_value.text='# Test README\nGenerated content.';mock_client.return_value=F;I=[L,'main_script.py','-p',B,'--name','Test Project','--description','A test project','--authors','Test Author','--overwrite'];G=O.run(I,capture_output=C,text=C);K(G.stdout);K(G.stderr);H=A.path.join(J,'main.md');self.assertTrue(A.path.exists(H),'README was not generated.')
		with D(H,'r',encoding=E)as M:P=M.read();self.assertIn('Test README',P)
if __name__=='__main__':H.main()