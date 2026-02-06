# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, tools, _


class LeaveReport(models.Model):
    _name = "hr.leave.employee.type.report"
    _description = 'Time Off Summary / Report'
    _auto = False
    _order = "date_from DESC, employee_id"

    employee_id = fields.Many2one('hr.employee', string="Employee", readonly=True)
    active_employee = fields.Boolean(related='employee_id.active', readonly=True)
    number_of_days = fields.Float('Number of Days', readonly=True, group_operator="sum")
    department_id = fields.Many2one('hr.department', string='Department', readonly=True)
    leave_type = fields.Many2one("hr.leave.type", string="Leave Type", readonly=True)
    holiday_status = fields.Selection([
        ('taken', 'Taken'), #taken = validated
        ('left', 'Left'),
        ('planned', 'Planned'),
        ('expired', 'Expired')
    ])
    state = fields.Selection([
        ('draft', 'To Submit'),
        ('cancel', 'Cancelled'),
        ('confirm', 'To Approve'),
        ('refuse', 'Refused'),
        ('validate1', 'Second Approval'),
        ('validate', 'Approved')
        ], string='Status', readonly=True)
    date_from = fields.Datetime('Start Date', readonly=True)
    date_to = fields.Datetime('End Date', readonly=True)
    company_id = fields.Many2one('res.company', string="Company", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self._cr, 'hr_leave_employee_type_report')

        self._cr.execute("""
CREATE or REPLACE view hr_leave_employee_type_report as (
                WITH RECURSIVE
                /* 1. Explode Leaves & Check for REAL Working Days (Handles China Tiao Xiu) */
                leaves_seq AS (
                    SELECT 
                        l.id as leave_id, l.employee_id, l.holiday_status_id,
                        l.number_of_days / GREATEST(1, COUNT(*) OVER (PARTITION BY l.id)) as day_value,
                        gs.day_date,
                        ROW_NUMBER() OVER (PARTITION BY l.employee_id, l.holiday_status_id ORDER BY l.date_from, l.id) as global_day_rn
                    FROM hr_leave l
                    CROSS JOIN LATERAL generate_series(l.date_from::date, l.date_to::date, '1 day'::interval) gs(day_date)
                    JOIN hr_employee e ON e.id = l.employee_id
                    WHERE l.state IN ('validate', 'validate1')
                      /* CHECK 1: Is this day defined in the employee's working schedule? */
                      AND (
                          EXISTS (
                              SELECT 1 FROM resource_calendar_attendance rca 
                              WHERE rca.calendar_id = e.resource_calendar_id 
                              /* Day_of_week in Odoo is 0 (Mon) to 6 (Sun). Postgres ISODOW is 1-7. */
                              AND CAST(rca.dayofweek AS INTEGER) = (EXTRACT(ISODOW FROM gs.day_date)::INTEGER - 1)
                          )
                          /* OR: Is it a specific 'Global' working day override (less common in Odoo but possible) */
                      )
                      /* CHECK 2: Ensure it's not a Public Holiday (rcl.resource_id IS NULL means global holiday) */
                      AND NOT EXISTS (
                          SELECT 1 FROM resource_calendar_leaves rcl 
                          WHERE (rcl.calendar_id = e.resource_calendar_id OR rcl.calendar_id IS NULL)
                          AND rcl.resource_id IS NULL
                          AND gs.day_date BETWEEN rcl.date_from::date AND rcl.date_to::date
                      )
                ),
                /* 2. Sequence Allocations for FIFO */
                alloc_seq AS (
                    SELECT 
                        id, private_name as name, employee_id, holiday_status_id, number_of_days as capacity,
                        date_from::date as d_from, date_to::date as d_to,
                        department_id, employee_company_id as company_id,
                        ROW_NUMBER() OVER (PARTITION BY employee_id, holiday_status_id ORDER BY date_to NULLS LAST, date_from, id) as rn
                    FROM hr_leave_allocation
                    WHERE state = 'validate' AND active = True
                ),
                /* 3. Recursive Engine with Safety Checks & Date Fit */
                consumption_trace(employee_id, holiday_status_id, day_rn, alloc_rn, remaining_day_val, remaining_alloc_cap, alloc_id) AS (
                    SELECT 
                        ls.employee_id, ls.holiday_status_id, ls.global_day_rn, asq.rn,
                        ls.day_value, asq.capacity, asq.id
                    FROM leaves_seq ls
                    JOIN alloc_seq asq ON ls.employee_id = asq.employee_id 
                        AND ls.holiday_status_id = asq.holiday_status_id
                        AND asq.rn = 1
                    WHERE ls.global_day_rn = 1

                    UNION ALL

                    SELECT 
                        t.employee_id, t.holiday_status_id,
                        -- LOGIC 1: Move to next leave day ONLY if current day is consumed
                        CASE
                            WHEN (ls.day_date BETWEEN asq.d_from AND COALESCE(asq.d_to, '9999-12-31'))
                                 AND t.remaining_day_val <= t.remaining_alloc_cap THEN t.day_rn + 1
                            WHEN (ls.day_date NOT BETWEEN asq.d_from AND COALESCE(asq.d_to, '9999-12-31')) THEN t.day_rn
                            ELSE t.day_rn
                        END,
                        -- LOGIC 2: Move to next allocation bucket ONLY if current is exhausted OR dates don't fit
                        CASE 
                            WHEN t.remaining_day_val > t.remaining_alloc_cap THEN t.alloc_rn + 1  -- Exhaustion first!
                            WHEN (ls.day_date NOT BETWEEN asq.d_from AND COALESCE(asq.d_to, '9999-12-31')) THEN t.alloc_rn + 1
                            ELSE t.alloc_rn 
                        END,
                        -- LOGIC 3: Fixed Remaining Leave Value
                        CASE 
                            -- Day fully consumed and moved on: fetch next day's value
                            WHEN t.remaining_day_val <= t.remaining_alloc_cap 
                                THEN COALESCE((SELECT day_value FROM leaves_seq WHERE global_day_rn = t.day_rn + 1 AND employee_id = t.employee_id AND holiday_status_id = t.holiday_status_id LIMIT 1), 0)
                            -- Day partially consumed (allocation exhausted): carry remainder to next allocation
                            WHEN t.remaining_day_val > t.remaining_alloc_cap
                                THEN t.remaining_day_val - t.remaining_alloc_cap
                            -- Date mismatch: keep full remainder for next allocation
                            ELSE t.remaining_day_val
                        END,
                        -- LOGIC 4: Fixed Remaining Allocation Capacity ( coupling check )
                        CASE
                            WHEN (ls.day_date NOT BETWEEN asq.d_from AND COALESCE(asq.d_to, '9999-12-31'))
                                THEN COALESCE((SELECT capacity FROM alloc_seq WHERE rn = t.alloc_rn + 1 AND employee_id = t.employee_id AND holiday_status_id = t.holiday_status_id LIMIT 1), 0) -- Fetch new allocation's capacity
                            WHEN t.remaining_day_val > t.remaining_alloc_cap
                                THEN COALESCE((SELECT capacity FROM alloc_seq WHERE rn = t.alloc_rn + 1 AND employee_id = t.employee_id AND holiday_status_id = t.holiday_status_id LIMIT 1), 0)
                            ELSE t.remaining_alloc_cap - t.remaining_day_val
                        END,
                        -- LOGIC 5: Allocation ID switch
                        CASE
                            WHEN (ls.day_date NOT BETWEEN asq.d_from AND COALESCE(asq.d_to, '9999-12-31'))
                                THEN (SELECT id FROM alloc_seq WHERE rn = t.alloc_rn + 1 AND employee_id = t.employee_id AND holiday_status_id = t.holiday_status_id LIMIT 1)
                            WHEN t.remaining_day_val > t.remaining_alloc_cap
                                THEN (SELECT id FROM alloc_seq WHERE rn = t.alloc_rn + 1 AND employee_id = t.employee_id AND holiday_status_id = t.holiday_status_id LIMIT 1)
                            ELSE t.alloc_id
                        END
                    FROM consumption_trace t
                    LEFT JOIN leaves_seq ls ON ls.global_day_rn = t.day_rn AND ls.employee_id = t.employee_id AND ls.holiday_status_id = t.holiday_status_id
                    LEFT JOIN alloc_seq asq ON asq.rn = t.alloc_rn AND asq.employee_id = t.employee_id AND asq.holiday_status_id = t.holiday_status_id
                    WHERE (t.day_rn <= (SELECT MAX(global_day_rn) FROM leaves_seq WHERE employee_id = t.employee_id AND holiday_status_id = t.holiday_status_id))
                      AND (t.alloc_rn <= (SELECT MAX(rn) FROM alloc_seq WHERE employee_id = t.employee_id AND holiday_status_id = t.holiday_status_id))
                ),
                final_consumption AS (
                    SELECT 
                        asq.id as alloc_id,
                        CASE 
                            -- Check the last step for this allocation
                            -- If remaining_day > remaining_alloc_cap, it was fully exhausted
                            WHEN (
                                SELECT t.remaining_day_val > t.remaining_alloc_cap
                                FROM consumption_trace t
                                WHERE t.alloc_id = asq.id
                                ORDER BY t.day_rn DESC, t.alloc_rn DESC
                                LIMIT 1
                            )
                            THEN asq.capacity
                            -- Otherwise, consumed = capacity - minimum remaining seen
                            ELSE GREATEST(
                                asq.capacity - (
                                    SELECT MIN(remaining_alloc_cap) 
                                    FROM consumption_trace 
                                    WHERE alloc_id = asq.id
                                ),
                                0
                            )
                        END as used_qty
                    FROM alloc_seq asq
                    WHERE EXISTS (SELECT 1 FROM consumption_trace WHERE alloc_id = asq.id)
                )

                SELECT row_number() over() as id,
                       employee_id, number_of_days, department_id, leave_type,
                       holiday_status, state, date_from, date_to, company_id, name
                FROM (
                    SELECT a.employee_id, a.number_of_days, a.department_id, a.holiday_status_id as leave_type, 
                           'left' as holiday_status, 'validate' as state, a.date_from, a.date_to, a.employee_company_id as company_id, a.private_name as name
                    FROM hr_leave_allocation a WHERE a.state = 'validate' AND a.active = True
                    UNION ALL
                    SELECT l.employee_id, l.number_of_days * -1, l.department_id, l.holiday_status_id as leave_type,
                           CASE WHEN l.state = 'confirm' THEN 'planned' ELSE 'taken' END as holiday_status,
                           l.state, l.date_from, l.date_to, l.employee_company_id as company_id, l.private_name as name
                    FROM hr_leave l WHERE l.state IN ('confirm', 'validate', 'validate1')
                    UNION ALL
                    SELECT 
                        ao.employee_id, 
                        (ao.capacity - COALESCE(fc.used_qty, 0)) * -1 as number_of_days,
                        ao.department_id, ao.holiday_status_id as leave_type,
                        'expired' as holiday_status, 'validate' as state,
                        ao.d_from::timestamp, ao.d_to::timestamp, ao.company_id,
                        'Expired Unused - ' || ao.name as name
                    FROM alloc_seq ao
                    LEFT JOIN final_consumption fc ON ao.id = fc.alloc_id
                    WHERE ao.d_to < CURRENT_DATE 
                      AND (ao.capacity - COALESCE(fc.used_qty, 0)) > 0.001
                ) final_report
            );
        """)

    @api.model
    def action_time_off_analysis(self):
        domain = []
        if self.env.context.get('active_ids'):
            domain = [('employee_id', 'in', self.env.context.get('active_ids', []))]

        return {
            'name': _('Time Off Analysis'),
            'type': 'ir.actions.act_window',
            'res_model': 'hr.leave.employee.type.report',
            'view_mode': 'pivot',
            'search_view_id': [self.env.ref('hr_holidays.view_search_hr_holidays_employee_type_report').id],
            'domain': domain,
            'context': {
                'search_default_year': True,
                'search_default_company': True,
                'search_default_employee': True,
                'group_expand': True,
            }
        }
