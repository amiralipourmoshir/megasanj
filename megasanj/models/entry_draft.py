"""One server-owned weighbridge draft per admitted vehicle."""
import hashlib,json
from odoo import api,fields,models
from .common import INTERNAL


class Document(models.Model):
    _inherit='megasanj.document'
    state=fields.Selection(selection_add=[('draft','پیش‌نویس')],ondelete={'draft':'set default'})
    auto_entry_draft=fields.Boolean('ساخته‌شده از ثبت ورود',readonly=True,copy=False)


class Waybill(models.Model):
    _inherit='megasanj.waybill'

    def _ms_ensure_entry_draft(self):
        self.ensure_one()
        self.env.cr.execute('SELECT id FROM megasanj_waybill WHERE id=%s FOR UPDATE',(self.id,))
        documents=self.env['megasanj.document'].sudo()
        existing=documents.search([('waybill_id','=',self.id)],order='id',limit=1)
        if existing or self.gate_state not in ('ready','inside') or self.state not in ('issued','reserved'):
            return existing
        destination=self.env['stock.location'].search([('company_id','=',self.company_id.id),('ms_category_id','=',self.category_id.id),('ms_receiving_default','=',True)],limit=1)
        event='gate:waybill:%s:%s'%(self.company_id.id,self.id)
        stamp=(self.entry_at or self.called_at or fields.Datetime.now()).isoformat()+'Z'
        header={'document_number':'WB-%08d'%self.number,'movement_type':'external_to_warehouse','registered_at':stamp}
        payload={'schema_version':3,'event_type':'weighbridge.capture','event_id':event,'document':header,
            'material_type_id':self.category_id.id,'material_policy_id':self.policy_id.id,
            'destination_warehouse_id':destination.id or None,'waybill':{'number':self.number},
            'driver':{'name':self.driver_name,'phone':self.driver_phone},'vehicle':{'trailer_plate':self.trailer_plate},
            'origin':{'province':self.origin_province,'city':self.origin_city},'weighings':[]}
        return documents.with_context(_ms_internal=INTERNAL).create({'name':header['document_number'],'event_id':event,
            'payload_hash':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),'payload':payload,
            'company_id':self.company_id.id,'category_id':self.category_id.id,'policy_id':self.policy_id.id,
            'policy_snapshot':self.policy_id.contract(),'movement_type':'external_to_warehouse','waybill_id':self.id,
            'destination_id':destination.id,'state':'draft','workflow_version':3,'auto_entry_draft':True})

    def gate_action(self,action):
        result=super().gate_action(action)
        if action in ('call','enter'):self._ms_ensure_entry_draft()
        return result

    @api.model
    def _ms_backfill_entry_drafts(self):
        for waybill in self.sudo().search([('gate_state','in',['ready','inside']),('state','in',['issued','reserved'])]):
            waybill._ms_ensure_entry_draft()
