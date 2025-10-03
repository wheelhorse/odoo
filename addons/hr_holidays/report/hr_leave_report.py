# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, tools, _
from odoo.osv import expression


class LeaveReport(models.Model):
    _name = "hr.leave.report"
    _description = 'Time Off Summary / Report'
    _auto = False
    _order = "date_from DESC, employee_id"

    employee_id = fields.Many2one('hr.employee', string="Employee", readonly=True)
    active_employee = fields.Boolean(related='employee_id.active', readonly=True)
    name = fields.Char('Description', readonly=True)
    number_of_days = fields.Float('Number of Days', readonly=True)
    leave_type = fields.Selection([
        ('allocation', 'Allocation'),
        ('request', 'Time Off'),
        ('expired', 'Expired Unused')
        ], string='Request Type', readonly=True)
    department_id = fields.Many2one('hr.department', string='Department', readonly=True)
    category_id = fields.Many2one('hr.employee.category', string='Employee Tag', readonly=True)
    holiday_status_id = fields.Many2one("hr.leave.type", string="Leave Type", readonly=True)
    state = fields.Selection([
        ('draft', 'To Submit'),
        ('cancel', 'Cancelled'),
        ('confirm', 'To Approve'),
        ('refuse', 'Refused'),
        ('validate1', 'Second Approval'),
        ('validate', 'Approved')
        ], string='Status', readonly=True)
    holiday_type = fields.Selection([
        ('employee', 'By Employee'),
        ('category', 'By Employee Tag')
    ], string='Allocation Mode', readonly=True)
    date_from = fields.Datetime('Start Date', readonly=True)
    date_to = fields.Datetime('End Date', readonly=True)
    company_id = fields.Many2one('res.company', string="Company", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self._cr, 'hr_leave_report')
        self._cr.execute("""
            CREATE OR REPLACE VIEW hr_leave_report AS (
                WITH all_rows AS (
                    -- Existing allocations
                    SELECT
                        alloc.id AS original_id,
                        alloc.employee_id,
                        alloc.private_name AS name,
                        alloc.number_of_days,
                        'allocation' AS leave_type,
                        alloc.category_id,
                        alloc.department_id,
                        alloc.holiday_status_id,
                        alloc.state,
                        alloc.holiday_type,
                        alloc.date_from,
                        alloc.date_to,
                        alloc.employee_company_id AS company_id
                    FROM hr_leave_allocation alloc

                    UNION ALL

                    -- Existing requests
                    SELECT
                        req.id AS original_id,
                        req.employee_id,
                        req.private_name AS name,
                        -req.number_of_days AS number_of_days,
                        'request' AS leave_type,
                        req.category_id,
                        req.department_id,
                        req.holiday_status_id,
                        req.state,
                        req.holiday_type,
                        req.date_from,
                        req.date_to,
                        req.employee_company_id AS company_id
                    FROM hr_leave req

                    UNION ALL

                    -- Calculated expired leaves using corrected FIFO logic - one entry per allocation
                    SELECT
                        expired.allocation_id AS original_id, -- Use actual allocation ID for ordering
                        expired.employee_id,
                        CONCAT('Expired Unused - ', expired.allocation_name) AS name,
                        -expired.unused_days AS number_of_days,
                        'expired' AS leave_type,
                        NULL AS category_id,
                        emp.department_id,
                        expired.holiday_status_id,
                        'validate' AS state,
                        expired.holiday_type,
                        expired.allocation_date_from AS date_from,
                        expired.allocation_date_to AS date_to,
                        expired.company_id
                    FROM (
                        WITH
                        -- Match each leave request to valid allocations based on date overlap
                        matched_requests AS (
                            SELECT 
                                req.id as request_id,
                                req.employee_id,
                                req.holiday_status_id,
                                req.number_of_days as request_days,
                                req.date_from as request_date_from,
                                alloc.id as allocation_id,
                                alloc.private_name as allocation_name,
                                alloc.number_of_days as allocation_days,
                                alloc.date_from as allocation_date_from,
                                alloc.date_to as allocation_date_to,
                                alloc.holiday_type,
                                alloc.employee_company_id as company_id,
                                -- Rank allocations by expiry date for FIFO consumption
                                ROW_NUMBER() OVER (PARTITION BY req.id ORDER BY alloc.date_to, alloc.id) as allocation_rank
                            FROM hr_leave req
                            JOIN hr_leave_allocation alloc ON 
                                req.employee_id = alloc.employee_id 
                                AND req.holiday_status_id = alloc.holiday_status_id
                                AND alloc.state = 'validate'
                                AND req.state IN ('validate', 'validate1')
                                -- Check if request date overlaps with allocation validity period
                                AND req.date_from >= alloc.date_from 
                                AND req.date_from <= alloc.date_to
                        ),
                        -- Calculate consumption for each allocation using FIFO
                        allocation_consumption AS (
                            SELECT 
                                employee_id,
                                holiday_status_id,
                                allocation_id,
                                allocation_name,
                                allocation_days,
                                allocation_date_from,
                                allocation_date_to,
                                holiday_type,
                                company_id,
                                SUM(request_days) as consumed_days
                            FROM matched_requests 
                            WHERE allocation_rank = 1  -- Only consider the first valid allocation for each request (FIFO)
                            GROUP BY employee_id, holiday_status_id, allocation_id, allocation_name, allocation_days, allocation_date_from, allocation_date_to, holiday_type, company_id
                        ),
                        -- Calculate unused days for each allocation
                        allocation_unused AS (
                            SELECT 
                                alloc.employee_id,
                                alloc.holiday_status_id,
                                alloc.id as allocation_id,
                                alloc.private_name as allocation_name,
                                alloc.number_of_days as allocation_days,
                                alloc.date_from as allocation_date_from,
                                alloc.date_to as allocation_date_to,
                                alloc.holiday_type,
                                alloc.employee_company_id as company_id,
                                COALESCE(cons.consumed_days, 0) as consumed_days,
                                alloc.number_of_days - COALESCE(cons.consumed_days, 0) as unused_days
                            FROM hr_leave_allocation alloc
                            LEFT JOIN allocation_consumption cons ON 
                                alloc.id = cons.allocation_id
                            WHERE alloc.state = 'validate'
                        )
                        -- Return individual allocation records that have expired unused days
                        SELECT
                            employee_id,
                            holiday_status_id,
                            allocation_id,
                            allocation_name,
                            allocation_date_from,
                            allocation_date_to,
                            holiday_type,
                            company_id,
                            unused_days
                        FROM allocation_unused
                        WHERE allocation_date_to < CURRENT_DATE AND unused_days > 0
                    ) AS expired
                    LEFT JOIN hr_employee emp ON expired.employee_id = emp.id
                )
                SELECT
                    row_number() OVER (ORDER BY employee_id, leave_type, original_id) AS id,
                    employee_id,
                    name,
                    number_of_days,
                    leave_type,
                    category_id,
                    department_id,
                    holiday_status_id,
                    state,
                    holiday_type,
                    date_from,
                    date_to,
                    company_id
                FROM all_rows
            );
        """)

    @api.model
    def action_time_off_analysis(self):
        domain = [('holiday_type', '=', 'employee')]

        if self.env.context.get('active_ids'):
            domain = expression.AND([
                domain,
                [('employee_id', 'in', self.env.context.get('active_ids', []))]
            ])

        return {
            'name': _('Time Off Analysis'),
            'type': 'ir.actions.act_window',
            'res_model': 'hr.leave.report',
            'view_mode': 'tree,pivot,form',
            'search_view_id': [self.env.ref('hr_holidays.view_hr_holidays_filter_report').id],
            'domain': domain,
            'context': {
                'search_default_group_type': True,
                'search_default_year': True,
                'search_default_validated': True,
                'search_default_active_employee': True,
            }
        }
