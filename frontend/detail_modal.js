/**
 * Global Universal Record Details Modal Controller
 * Sagar Accounts Software
 */

function showRecordDetails(config) {
    const modal = document.getElementById('globalDetailModal');
    if (!modal) return;

    // 1. Set Title & Subtitle
    const titleEl = document.getElementById('globalDetailModalTitle');
    const subtitleEl = document.getElementById('globalDetailModalSubtitle');
    if (titleEl) titleEl.textContent = config.title || 'Record Details';
    if (subtitleEl) subtitleEl.textContent = config.subtitle || '';

    // 2. Set Icon & Color
    const iconContainer = document.getElementById('globalDetailModalIcon');
    if (iconContainer) {
        const iconClass = config.icon || 'fas fa-info-circle';
        const iconColor = config.iconColor || 'var(--primary)';
        iconContainer.innerHTML = `<i class="${iconClass}"></i>`;
        iconContainer.style.color = iconColor;
        iconContainer.style.borderColor = iconColor;
    }

    // 3. Set Badge (if any)
    const badgeEl = document.getElementById('globalDetailModalBadge');
    if (badgeEl) {
        if (config.badge && config.badge.text) {
            badgeEl.className = 'badge ' + (config.badge.class || 'badge-info');
            badgeEl.textContent = config.badge.text;
            badgeEl.style.display = 'inline-block';
        } else {
            badgeEl.style.display = 'none';
        }
    }

    // 4. Populate Sections
    const bodyEl = document.getElementById('globalDetailModalBody');
    if (bodyEl) {
        bodyEl.innerHTML = '';
        if (config.sections && Array.isArray(config.sections)) {
            config.sections.forEach(sec => {
                const sectionCard = document.createElement('div');
                sectionCard.className = 'detail-section-card';

                // Section Header
                if (sec.title) {
                    const secHeader = document.createElement('div');
                    secHeader.className = 'detail-section-header';
                    const secIcon = sec.icon ? `<i class="${sec.icon}"></i> ` : '';
                    secHeader.innerHTML = `<h4>${secIcon}${sec.title}</h4>`;
                    if (sec.badge) {
                        secHeader.innerHTML += `<span class="badge ${sec.badge.class || 'badge-secondary'}" style="font-size: 0.7rem;">${sec.badge.text}</span>`;
                    }
                    sectionCard.appendChild(secHeader);
                }

                // Section Fields Grid (if fields provided)
                if (sec.fields && Array.isArray(sec.fields)) {
                    const grid = document.createElement('div');
                    grid.className = 'detail-grid';
                    sec.fields.forEach(f => {
                        if (f.value === undefined || f.value === null || f.value === '') {
                            f.value = '—';
                        }
                        const fieldEl = document.createElement('div');
                        fieldEl.className = 'detail-field' + (f.fullWidth ? ' detail-field-full' : '');

                        const labelEl = document.createElement('div');
                        labelEl.className = 'detail-label';
                        labelEl.textContent = f.label;

                        const valueEl = document.createElement('div');
                        valueEl.className = 'detail-value';

                        if (f.badgeClass) {
                            valueEl.innerHTML = `<span class="badge ${f.badgeClass}">${f.value}</span>`;
                        } else if (f.code) {
                            valueEl.innerHTML = `<span class="table-code">${f.value}</span>`;
                        } else {
                            valueEl.textContent = f.value;
                        }

                        if (f.color) valueEl.style.color = f.color;
                        if (f.bold) valueEl.style.fontWeight = '700';

                        fieldEl.appendChild(labelEl);
                        fieldEl.appendChild(valueEl);
                        grid.appendChild(fieldEl);
                    });
                    sectionCard.appendChild(grid);
                }

                // Section Table (if tabular list provided)
                if (sec.table && Array.isArray(sec.table.headers) && Array.isArray(sec.table.rows)) {
                    const tableWrap = document.createElement('div');
                    tableWrap.className = 'table-responsive';
                    tableWrap.style.marginTop = '10px';
                    tableWrap.style.maxHeight = '320px';
                    tableWrap.style.overflowY = 'auto';

                    let tableHtml = `<table class="data-table" style="font-size: 0.82rem; margin: 0;"><thead><tr>`;
                    sec.table.headers.forEach(h => {
                        const align = h.align ? `text-align: ${h.align};` : '';
                        const width = h.width ? `width: ${h.width};` : '';
                        tableHtml += `<th style="${align} ${width}">${h.label}</th>`;
                    });
                    tableHtml += `</tr></thead><tbody>`;

                    if (sec.table.rows.length === 0) {
                        tableHtml += `<tr><td colspan="${sec.table.headers.length}" class="text-center" style="padding: 24px; color: var(--text-muted);">No transaction records found for this period.</td></tr>`;
                    } else {
                        sec.table.rows.forEach(r => {
                            tableHtml += `<tr>`;
                            r.forEach((cell, idx) => {
                                const h = sec.table.headers[idx] || {};
                                const align = h.align ? `text-align: ${h.align};` : '';
                                tableHtml += `<td style="${align}">${cell}</td>`;
                            });
                            tableHtml += `</tr>`;
                        });
                    }

                    tableHtml += `</tbody></table>`;
                    tableWrap.innerHTML = tableHtml;
                    sectionCard.appendChild(tableWrap);
                }

                bodyEl.appendChild(sectionCard);
            });
        }
    }

    // 5. Populate Custom Actions
    const customActionsContainer = document.getElementById('globalDetailModalCustomActions');
    if (customActionsContainer) {
        customActionsContainer.innerHTML = '';
        if (config.actions && Array.isArray(config.actions)) {
            config.actions.forEach(act => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'btn ' + (act.class || 'btn-primary');
                btn.style.fontSize = '0.85rem';
                btn.style.padding = '8px 18px';
                btn.innerHTML = (act.icon ? `<i class="${act.icon}"></i> ` : '') + act.label;
                btn.onclick = () => {
                    closeRecordDetails();
                    if (typeof act.onClick === 'function') {
                        act.onClick();
                    } else if (typeof act.onClick === 'string') {
                        eval(act.onClick);
                    }
                };
                customActionsContainer.appendChild(btn);
            });
        }
    }

    // 6. Open Modal
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
}

function closeRecordDetails() {
    const modal = document.getElementById('globalDetailModal');
    if (modal) {
        modal.style.display = 'none';
        document.body.style.overflow = '';
    }
}

function handleModalBackdropClick(event) {
    if (event.target.id === 'globalDetailModal') {
        closeRecordDetails();
    }
}

// Global Escape Key Listener
document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' || e.keyCode === 27) {
        const modal = document.getElementById('globalDetailModal');
        if (modal && modal.style.display === 'flex') {
            closeRecordDetails();
        }
    }
});

/**
 * Universal Accounting Drill-Down Controller
 * Fetches underlying transactions for any account code and displays in the global modal
 */
function showDrilldown(accountCode, accountName, startDate, endDate) {
    let url = `/api/reports/drilldown?account_code=${encodeURIComponent(accountCode)}`;
    if (startDate) url += `&start_date=${encodeURIComponent(startDate)}`;
    if (endDate) url += `&end_date=${encodeURIComponent(endDate)}`;

    fetch(url)
        .then(r => r.json())
        .then(res => {
            if (res.status !== 'success' || !res.data) {
                alert('Could not retrieve drill-down records: ' + (res.message || 'Account not found'));
                return;
            }

            const data = res.data;
            const acc = data.account;
            const txns = data.transactions || [];

            const formattedNet = '₹' + Number(Math.abs(data.net_balance)).toLocaleString('en-IN', {minimumFractionDigits: 2});
            const balanceNote = data.net_balance >= 0 ? `${formattedNet} (${acc.normal_balance})` : `${formattedNet} (Opposite)`;

            const rows = txns.map(t => [
                `<span style="color: var(--text-muted); font-size: 0.78rem;">${t.entry_date}</span>`,
                `<span class="table-code">${t.entry_number}</span>`,
                `<strong>${t.reference_no}</strong>`,
                `<span class="source-badge source-${t.source_module}" style="font-size: 0.68rem; padding: 2px 6px;">${t.source_module}</span>`,
                `<span title="${t.narration}">${t.narration.length > 35 ? t.narration.substring(0, 35) + '...' : t.narration}</span>`,
                t.debit > 0 ? `<span style="color: #34d399; font-weight: 600;">₹${Number(t.debit).toLocaleString('en-IN', {minimumFractionDigits: 2})}</span>` : '—',
                t.credit > 0 ? `<span style="color: #f87171; font-weight: 600;">₹${Number(t.credit).toLocaleString('en-IN', {minimumFractionDigits: 2})}</span>` : '—',
                `<strong>₹${Number(t.running_balance).toLocaleString('en-IN', {minimumFractionDigits: 2})}</strong>`
            ]);

            showRecordDetails({
                title: `[${acc.code}] ${acc.name}`,
                subtitle: `${acc.major_type} • ${acc.sub_type} • Accounting Drill-Down`,
                icon: 'fas fa-search-dollar',
                iconColor: 'var(--primary)',
                badge: {
                    text: `${txns.length} Entry${txns.length !== 1 ? 's' : ''}`,
                    class: 'badge-info'
                },
                sections: [
                    {
                        title: 'Account & Period Summary',
                        icon: 'fas fa-info-circle',
                        fields: [
                            { label: 'Account Code', value: acc.code, code: true },
                            { label: 'Classification', value: `${acc.major_type} (${acc.sub_type})` },
                            { label: 'Reporting Period', value: `${data.period.start_date} to ${data.period.end_date}` },
                            { label: 'Total Debits (Dr)', value: '₹' + Number(data.total_debit).toLocaleString('en-IN', {minimumFractionDigits: 2}), color: '#34d399', bold: true },
                            { label: 'Total Credits (Cr)', value: '₹' + Number(data.total_credit).toLocaleString('en-IN', {minimumFractionDigits: 2}), color: '#f87171', bold: true },
                            { label: 'Net Position', value: balanceNote, color: 'var(--accent)', bold: true }
                        ]
                    },
                    {
                        title: 'Contributing Journal Postings',
                        icon: 'fas fa-list-ol',
                        table: {
                            headers: [
                                { label: 'Date', width: '120px' },
                                { label: 'Voucher #', width: '110px' },
                                { label: 'Ref #', width: '100px' },
                                { label: 'Source', width: '80px' },
                                { label: 'Narration / Description' },
                                { label: 'Debit (₹)', align: 'right', width: '95px' },
                                { label: 'Credit (₹)', align: 'right', width: '95px' },
                                { label: 'Balance (₹)', align: 'right', width: '100px' }
                            ],
                            rows: rows
                        }
                    }
                ],
                actions: [
                    {
                        label: 'View in General Ledger',
                        icon: 'fas fa-book-open',
                        class: 'btn-secondary',
                        onClick: () => {
                            window.location.href = `/ledger?account_id=${encodeURIComponent(acc.code)}`;
                        }
                    }
                ]
            });
        })
        .catch(err => {
            alert('Error fetching drilldown details: ' + err);
        });
}
