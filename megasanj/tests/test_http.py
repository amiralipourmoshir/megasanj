import json
from odoo.tests import HttpCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install','-at_install')
class TestMegaSanjHttp(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = new_test_user(cls.env,login='ms_http_test',password='MS-Only-In-Test-983!',groups='megasanj.group_manager')

    def call(self,path,data=None,token=None):
        headers={'Content-Type':'application/json','Accept':'application/json'}
        if token: headers['Authorization']='Bearer '+token
        response=self.url_open(path,data=json.dumps(data) if data is not None else None,headers=headers)
        return response.status_code,response.json()

    def test_bearer_login_reference_session_revoke(self):
        status,data=self.call('/api/login',{'email':self.operator.login,'password':'MS-Only-In-Test-983!','client':'weighbridge'})
        self.assertEqual(status,200,data)
        token=data['data']['token']
        self.assertEqual(data['data']['user']['edition'],'odoo19')
        status,profile=self.call('/api/weighbridge/session',token=token)
        self.assertEqual(status,200,profile)
        self.assertEqual(profile['data']['id'],self.operator.id)
        status,refs=self.call('/api/transportation/reference/materials',token=token)
        self.assertEqual(status,200,refs)
        self.assertEqual(refs['schema_version'],3)
        status,unchanged=self.call('/api/transportation/reference/materials?known_version='+refs['snapshot_version'],token=token)
        self.assertTrue(unchanged['not_modified'])
        self.assertIsNone(unchanged['data'])
        for path in ['/api/megasanj/receipts','/api/megasanj/productions','/api/transportation/truck-queue','/api/transportation/desktop-sync']:
            status,content=self.call(path,token=token)
            self.assertEqual(status,200,content)
            self.assertTrue(content['success'])
        self.assertEqual(self.call('/api/logout',{},token)[0],200)
        self.assertEqual(self.call('/api/weighbridge/session',token=token)[0],401)

    def test_missing_token_and_malformed_weighing(self):
        self.assertEqual(self.call('/api/megasanj/receipts')[0],401)
        status,profile=self.call('/api/login',{'email':self.operator.login,'password':'MS-Only-In-Test-983!'})
        self.assertEqual(status,200,profile)
        status,data=self.call('/api/transportation/v3/weighbridge',{},profile['data']['token'])
        self.assertEqual(status,422,data)
