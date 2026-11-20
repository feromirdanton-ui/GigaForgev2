const BOARD_WIDTH = 10;
const BOARD_HEIGHT = 20;

const TETROMINOS = [
    [0, 1, 1, 0, 0, 1, 1, 0], // O
    [1, 1, 1, 1],              // I
    [1, 1, 0, 1, 1, 0, 0, 0], // Z
    [0, 1, 1, 1, 1, 0, 0, 0], // L
    [1, 1, 1, 0, 1, 0, 0, 0], // J
    [0, 1, 1, 1, 0, 1, 0, 0], // T
    [1, 1, 1, 1, 0, 0, 0, 0]  // S
];

class Piece {
    constructor(type, x, y) {
        this.type = type;
        this.x = x || Math.floor(BOARD_WIDTH / 2) - 1;
        this.y = y || 0;
        this.rotation = 0;
    }

    getShape() {
        const shapeIndex = this.type * 4 + this.rotation % 4;
        return TETROMINOS[shapeIndex].slice();
    }
}

function createBoard() {
    let board = [];
    for (let i = 0; i < BOARD_HEIGHT; i++) {
        board.push(new Array(BOARD_WIDTH).fill(0));
    }
    return board;
}

function collides(board, piece) {
    const shape = piece.getShape();
    for (let row = 0; row < shape.length; row += 4) {
        for (let col = 0; col < 4; col++) {
            if (shape[row + col]) {
                const x = piece.x + col;
                const y = piece.y + Math.floor(row / 4);
                if (
                    x < 0 ||
                    x >= BOARD_WIDTH ||
                    y >= BOARD_HEIGHT ||
                    (y >= 0 && board[y][x])
                ) {
                    return true;
                }
            }
        }
    }
    return false;
}

function mergePieceToBoard(board, piece) {
    const shape = piece.getShape();
    for (let row = 0; row < shape.length; row += 4) {
        for (let col = 0; col < 4; col++) {
            if (shape[row + col]) {
                const x = piece.x + col;
                const y = piece.y + Math.floor(row / 4);
                if (y >= 0) {
                    board[y][x] = piece.type + 1;
                }
            }
        }
    }
}

function removeFullRows(board) {
    let rowsRemoved = 0;
    for (let y = BOARD_HEIGHT - 1; y >= 0; y--) {
        let fullRow = true;
        for (let x = 0; x < BOARD_WIDTH; x++) {
            if (!board[y][x]) {
                fullRow = false;
                break;
            }
        }
        if (fullRow) {
            rowsRemoved++;
            board.splice(y, 1);
            board.unshift(new Array(BOARD_WIDTH).fill(0));
        }
    }
    return rowsRemoved;
}

function newBag() {
    return [...Array(TETROMINOS.length).keys()].sort(() => Math.random() - 0.5);
}

function renderBoard(ctx, board) {
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    for (let y = 0; y < BOARD_HEIGHT; y++) {
        for (let x = 0; x < BOARD_WIDTH; x++) {
            if (board[y][x]) {
                ctx.fillStyle = `hsl(${board[y][x] * 40}, 100%, 50%)`;
                ctx.fillRect(x * 20, y * 20, 20, 20);
            }
        }
    }
}

function renderNextPiece(ctx, piece) {
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    const shape = piece.getShape();
    for (let row = 0; row < shape.length; row += 4) {
        for (let col = 0; col < 4; col++) {
            if (shape[row + col]) {
                ctx.fillStyle = `hsl(${(piece.type + 1) * 40}, 100%, 50%)`;
                ctx.fillRect(col * 20, Math.floor(row / 4) * 20, 20, 20);
            }
        }
    }
}

function renderHoldPiece(ctx, piece) {
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    if (piece) {
        const shape = piece.getShape();
        for (let row = 0; row < shape.length; row += 4) {
            for (let col = 0; col < 4; col++) {
                if (shape[row + col]) {
                    ctx.fillStyle = `hsl(${(piece.type + 1) * 40}, 100%, 50%)`;
                    ctx.fillRect(col * 20, Math.floor(row / 4) * 20, 20, 20);
                }
            }
        }
    }
}

function updateUI(scoreElement, levelElement, linesElement, score, level, linesCleared) {
    scoreElement.textContent = `Score: ${score}`;
    levelElement.textContent = `Level: ${level}`;
    linesElement.textContent = `Lines Cleared: ${linesCleared}`;
}

function handleInput(event, state) {
    switch (event.keyCode) {
        case 65: // Left arrow
            event.preventDefault();
            if (!collides(state.board, { ...state.currentPiece, x: state.currentPiece.x - 1 })) {
                state.currentPiece.x--;
            }
            break;
        case 68: // Right arrow
            event.preventDefault();
            if (!collides(state.board, { ...state.currentPiece, x: state.currentPiece.x + 1 })) {
                state.currentPiece.x++;
            }
            break;
        case 87: // Up arrow
            event.preventDefault();
            const rotatedPiece = { ...state.currentPiece };
            rotatedPiece.rotation++;
            if (!collides(state.board, rotatedPiece)) {
                state.currentPiece.rotation++;
            }
            break;
        case 83: // Down arrow
            event.preventDefault();
            if (!collides(state.board, { ...state.currentPiece, y: state.currentPiece.y + 1 })) {
                state.currentPiece.y++;
            }
            break;
        case 32: // Space bar
            event.preventDefault();
            while (!collides(state.board, { ...state.currentPiece, y: state.currentPiece.y + 1 })) {
                state.currentPiece.y++;
            }
            break;
        case 80: // Pause key
            event.preventDefault();
            state.paused = !state.paused;
            document.getElementById('status').textContent = state.paused ? 'Paused' : '';
            break;
    }
}

function mainLoop(state, timestamp) {
    if (state.gameOver) return;

    if (!state.paused) {
        if (timestamp - state.lastDrop > state.dropInterval) {
            if (!collides(state.board, { ...state.currentPiece, y: state.currentPiece.y + 1 })) {
                state.currentPiece.y++;
            } else {
                mergePieceToBoard(state.board, state.currentPiece);
                const rowsRemoved = removeFullRows(state.board);
                state.score += rowsRemoved ** 2 * 100;
                state.linesCleared += rowsRemoved;
                state.level = Math.floor((state.linesCleared / 10) + 1);
                state.dropInterval = 1000 - (state.level * 50);
                state.currentPiece = state.nextPiece;
                state.nextPiece = new Piece(state.bag.shift());
                if (state.bag.length === 0) {
                    state.bag = newBag();
                }
                if (collides(state.board, state.currentPiece)) {
                    state.gameOver = true;
                    document.getElementById('status').textContent = 'Game Over';
                    return;
                }
            }
            state.lastDrop = timestamp;
        }
    }

    renderBoard(document.getElementById('game-board').getContext('2d'), state.board);
    renderNextPiece(document.getElementById('next-piece').getContext('2d'), state.nextPiece);
    renderHoldPiece(document.getElementById('hold-piece').getContext('2d'), state.holdPiece);
    updateUI(
        document.getElementById('score'),
        document.getElementById('level'),
        document.getElementById('lines'),
        state.score,
        state.level,
        state.linesCleared
    );

    window.requestAnimationFrame(mainLoop.bind(null, state));
}

document.addEventListener('keydown', function(event) {
    handleInput(event, gameState);
});

document.getElementById('start-button').addEventListener('click', () => {
    gameState.started = true;
    gameState.paused = false;
    document.getElementById('status').textContent = '';
    mainLoop(gameState, performance.now());
});

document.getElementById('pause-button').addEventListener('click', () => {
    gameState.paused = !gameState.paused;
    document.getElementById('status').textContent = gameState.paused ? 'Paused' : '';
});

document.getElementById('restart-button').addEventListener('click', () => {
    location.reload();
});

window.onload = () => {
    const gameState = {
        started: false,
        paused: false,
        gameOver: false,
        bag: newBag(),
        board: createBoard(),
        currentPiece: null,
        nextPiece: null,
        holdPiece: null,
        heldThisTurn: false,
        score: 0,
        level: 1,
        linesCleared: 0,
        lastDrop: 0,
        dropInterval: 1000
    };

    gameState.currentPiece = new Piece(gameState.bag.pop(), 0, 0);
    gameState.nextPiece = new Piece(gameState.bag.pop(), 0, 0);

    const gameBoardCanvas = document.getElementById('game-board');
    gameBoardCanvas.width = BOARD_WIDTH * 20;
    gameBoardCanvas.height = BOARD_HEIGHT * 20;

    const nextPieceCanvas = document.getElementById('next-piece');
    nextPieceCanvas.width = 80;
    nextPieceCanvas.height = 80;

    const holdPieceCanvas = document.getElementById('hold-piece');
    holdPieceCanvas.width = 80;
    holdPieceCanvas.height = 80;

    renderBoard(gameBoardCanvas.getContext('2d'), gameState.board);
    renderNextPiece(nextPieceCanvas.getContext('2d'), gameState.nextPiece);
};
