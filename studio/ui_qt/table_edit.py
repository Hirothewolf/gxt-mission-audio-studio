"""Direct GXT editing without bypassing the document codec or save history."""
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QAbstractItemDelegate, QPlainTextEdit, QStyledItemDelegate


class GxtTextDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = QPlainTextEdit(parent)
        editor.setObjectName('inlineGxtText')
        editor.setTabChangesFocus(True)
        editor.setToolTip('Enter aplica · Shift+Enter cria uma linha · Esc cancela')
        return editor

    def setEditorData(self, editor, index):
        editor.setPlainText(index.data(Qt.ItemDataRole.EditRole) or '')
        editor.selectAll()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        rect = option.rect
        rect.setHeight(max(rect.height(), 64))
        editor.setGeometry(rect)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.RevertModelCache)
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.commitData.emit(editor)
                self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)
                return True
        return super().eventFilter(editor, event)
