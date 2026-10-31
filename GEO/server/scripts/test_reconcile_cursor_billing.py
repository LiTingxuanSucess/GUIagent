import json
from pathlib import Path
import tempfile
import unittest
from scripts.reconcile_cursor_billing import reconcile


class BillingAuditTests(unittest.TestCase):
    def test_only_provider_failure_is_corrected_and_original_is_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=root/'data'
            (data/'cursor-logs').mkdir(parents=True);(data/'cursor-calls').mkdir()
            original={'status':'success','ok':True,'verified':True,'resume_ok':True,'elapsed_ms':1234}
            for i,line in [(1,'ActionRequiredError: You have an unpaid invoice\n'),(2,json.dumps({'type':'user','message':'unpaid invoice'})+'\n')]:
                ident=f'{i:024x}'
                (data/'cursor-calls'/f'{ident}.json').write_text(json.dumps(original),encoding='utf-8')
                (data/'cursor-logs'/f'{ident}.log').write_text(line,encoding='utf-8')
            self.assertEqual(reconcile(data)['count'],1)
            self.assertEqual(json.loads((data/'cursor-calls'/f'{1:024x}.json').read_text()),original)
            backup=root/'backup';self.assertEqual(reconcile(data,backup)['count'],1)
            updated=json.loads((data/'cursor-calls'/f'{1:024x}.json').read_text())
            self.assertEqual((updated['status'],updated['cli_assisted'],updated['verified'],updated['resume_ok']),('failed',False,True,True))
            self.assertEqual(json.loads((backup/f'{1:024x}.json').read_text()),original)
            self.assertEqual(reconcile(data,backup)['count'],0)


if __name__=='__main__':unittest.main()
